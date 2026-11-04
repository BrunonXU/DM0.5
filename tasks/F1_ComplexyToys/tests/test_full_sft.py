"""Full-specific parameter and export checks without allocating the VLA."""
import importlib.util
import json
from pathlib import Path
import sys
import pytest
import torch
from safetensors.torch import save_file, load_file
from f1_toys.full import audit_full

TASK = Path(__file__).resolve().parents[1]


def test_full_audit_rejects_frozen_parameters(tmp_path, monkeypatch):
    monkeypatch.setenv("F1_TOYS_RUN", str(tmp_path))
    model = torch.nn.Linear(3, 2)
    assert audit_full(model)["trainable_parameters"] == 8
    model.bias.requires_grad_(False)
    with pytest.raises(ValueError):
        audit_full(model)


@pytest.mark.parametrize("bad_inactive", [False, True])
def test_full_export_exact_and_inactive_guard(tmp_path, monkeypatch, bad_inactive):
    spec = importlib.util.spec_from_file_location("export_eef_test", TASK / "scripts/export_eef.py")
    export = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export)
    base = tmp_path / "base"
    checkpoint = tmp_path / "checkpoint/model_state_dict"
    dataset = tmp_path / "dataset"
    core = tmp_path / "rlinf/rlinf/models/embodiment/openpi_rlinf/pi0_model"
    for d in (base, checkpoint, dataset, core):
        d.mkdir(parents=True)
    (core / "__init__.py").write_text("")
    weights = {"action_out_proj.weight": torch.zeros(32, 4),
               "action_out_proj.bias": torch.zeros(32),
               "action_in_proj.weight": torch.zeros(4, 32)}
    save_file(weights, str(base / "model.safetensors"))
    updated = {k: v.clone() for k, v in weights.items()}
    updated["action_out_proj.weight"][:10] += 1
    if bad_inactive:
        updated["action_out_proj.bias"][10] = 1
    torch.save({"model." + k: v for k, v in updated.items()}, checkpoint / "full_weights.pt")
    (dataset / "contract.json").write_text((TASK / "configs/eef_v1_home_mask.json").read_text())
    (dataset / "norm_stats.json").write_text("{}")
    (dataset / "manifest.json").write_text("{}")
    token = tmp_path / "tokenizer"
    token.write_bytes(b"test")
    output = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["export", "--full", "--checkpoint", str(checkpoint.parent),
        "--dataset", str(dataset), "--rlinf-root", str(tmp_path / "rlinf"), "--tokenizer", str(token),
        "--base", str(base), "--output", str(output)])
    if bad_inactive:
        with pytest.raises(AssertionError):
            export.main()
    else:
        export.main()
        result = load_file(str(output / "model.safetensors"))
        assert all(torch.equal(result[k], v) for k, v in updated.items())
        manifest = json.loads((output / "bundle.json").read_text())
        assert manifest["training_mode"] == "full"
        assert not (output / "lora_manifest.json").exists()
