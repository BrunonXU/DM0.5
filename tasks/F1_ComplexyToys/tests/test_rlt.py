"""Contract checks for the frozen RLT adapter, using a small real RLinf module."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.rlt import PrefixCache, reconstruction_mse, select_anchors, token_class, validate_splits
from f1_toys.processing import sha256

CORE = TASK.parents[1] / "RLinf"
spec = importlib.util.spec_from_file_location("rlt_stage1_test_runner", TASK / "scripts/rlt_stage1.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def small_module():
    torch.manual_seed(7)
    return token_class(CORE)(input_dim=8, embed_dim=8, prefix_seq_len=6,
                            num_layers=1, num_heads=2, mlp_ratio=2).eval()


def test_masked_left_image_and_padding_cannot_change_token_or_loss():
    module = small_module()
    x = torch.randn(2, 6, 8)
    mask = torch.tensor([[0, 0, 1, 1, 1, 0], [0, 0, 1, 1, 1, 1]], dtype=torch.bool)
    changed = x.clone()
    changed[~mask] = 1000 * torch.randn_like(changed[~mask])
    torch.testing.assert_close(module.encode(x, mask), module.encode(changed, mask), rtol=1e-5, atol=1e-5)
    torch.testing.assert_close(module.loss(x, mask)[0], module.loss(changed, mask)[0], rtol=1e-5, atol=1e-5)


def test_reconstruction_does_not_backpropagate_to_vla_features():
    module = small_module()
    x = torch.randn(2, 6, 8, requires_grad=True)
    loss, _ = module.loss(x, torch.ones(2, 6, dtype=torch.bool))
    loss.backward()
    assert x.grad is None
    for block in (module.encoder, module.decoder):
        assert any(p.grad is not None and p.grad.abs().sum() > 0 for p in block.parameters())


def test_selected_anchors_cover_each_episode_without_duplicates():
    rows = [{"episode_id": e} for e, n in (("a", 20), ("b", 3)) for _ in range(n)]
    selected = select_anchors(rows, 5)
    assert len(selected) == len(set(selected)) == 8
    assert {0, 19, 20, 22}.issubset(selected)


@pytest.mark.parametrize("leak_key", ["episode", "source_group"])
def test_split_rejects_episode_or_session_leakage(leak_key):
    base = {"split_sha256":"s", "norm_stats_sha256":"n", "contract_sha256":"c"}
    a = {"episode":"a", "source_group":"sa"}
    b = {"episode":"b", "source_group":"sb"}
    b[leak_key] = a[leak_key]
    train = SimpleNamespace(manifest={**base, "split":"train", "source_episodes":[a]})
    val = SimpleNamespace(manifest={**base, "split":"validation", "source_episodes":[b]})
    with pytest.raises(ValueError, match="leakage"):
        validate_splits(train, val)


def test_bfloat_cache_roundtrip_and_corruption_detection(tmp_path):
    x = torch.randn(3, 6, 8).bfloat16()
    x.view(torch.uint16).numpy().tofile(tmp_path / "train.bf16")
    masks = np.ones((3,6), dtype=bool)
    np.save(tmp_path / "train-masks.npy", masks)
    files = {name:sha256(tmp_path/name) for name in ("train.bf16", "train-masks.npy")}
    manifest = {"schema":"f1_rlt_prefix_cache_v1", "splits":{"train":{
        "shape":list(x.shape), "records":[{}]*3, "files_sha256":files}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    cache = PrefixCache(tmp_path, "train")
    read, mask = cache.batch([2,0], "cpu")
    assert torch.equal(read, x[[2,0]]) and mask.all()
    with (tmp_path / "train.bf16").open("r+b") as stream:
        stream.write(b"bad")
    with pytest.raises(ValueError, match="integrity"):
        PrefixCache(tmp_path, "train")


def test_episode_sampler_restores_next_draw_and_exact_source_mix():
    records = [{"episode_id":str(i//3), "dataset_role":"original_demo" if i<9 else "correction"} for i in range(15)]
    g = torch.Generator().manual_seed(9)
    recipe = {"original_demo":.75, "correction":.25}
    runner.sample_indices(records, 8, g, recipe)
    state = g.get_state()
    first = runner.sample_indices(records, 8, g, recipe)
    g.set_state(state)
    second = runner.sample_indices(records, 8, g, recipe)
    assert first == second
    assert sum(records[i]["dataset_role"] == "original_demo" for i in first) == 6


def test_metric_excludes_invalid_coordinates_in_prefix_sequence():
    x = torch.ones(2, 6, 8)
    reconstruction = x.clone()
    mask = torch.ones(2, 6, dtype=torch.bool)
    mask[:, :2] = False
    reconstruction[:, :2] = 1000
    assert torch.equal(reconstruction_mse(reconstruction, x, mask), torch.zeros(2))
