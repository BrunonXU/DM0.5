import importlib.util
from pathlib import Path
import random
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from f1_toys.validation import run_validation

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_formal.py"
spec = importlib.util.spec_from_file_location("prepare_formal", SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_split_requires_exact_nonoverlapping_source_versions():
    summary = {"episodes": [{"episode": "a", "source_manifest_sha256": "ha"},
                            {"episode": "b", "source_manifest_sha256": "hb"}]}
    split = {"train": [{"episode": "a", "manifest_sha256": "ha"}],
             "validation": [{"episode": "b", "manifest_sha256": "hb"}]}
    prepare.validate_split(split, summary)
    split["validation"] = split["train"]
    with pytest.raises(ValueError):
        prepare.validate_split(split, summary)
    split["validation"] = [{"episode": "b", "manifest_sha256": "changed"}]
    with pytest.raises(ValueError):
        prepare.validate_split(split, summary)


def test_stats_deduplicate_with_episode_identity():
    states = np.array([[0.] * 16, [0.] * 16, [10.] * 16], np.float32)
    actions = np.zeros((3, 50, 10), np.float32)
    rows = [dict(episode_id="a", observation_id=1), dict(episode_id="a", observation_id=1),
            dict(episode_id="b", observation_id=1)]
    stats = prepare.fit_statistics(states, actions, rows)
    assert stats["state"]["unique_observations"] == 2
    assert stats["state"]["low"][7] == 0 and stats["state"]["high"][7] == 10
    assert stats["source_episodes"] == ["a", "b"]
    assert stats["scope"] == "train_only"


def test_short_validation_is_distinct_and_old_linspace_is_unchanged():
    for count in (1, 41, 95, 96):
        assert prepare.validation_indices("train", count) == []
    with pytest.raises(ValueError):
        prepare.validation_indices("train", 0)
    assert prepare.validation_indices("validation", 95) == list(range(95))
    assert prepare.validation_indices("validation", 1, 7) == [7]
    assert prepare.validation_indices("validation", 997) == np.linspace(0, 996, 96, dtype=int).tolist()
    assert prepare.validation_indices("validation", 96, 7) == list(range(7, 103))


def test_statistics_never_use_padding():
    states = np.zeros((2, 16), np.float32)
    rows = [dict(episode_id="a", observation_id=i) for i in range(2)]
    actions = np.full((2, 50, 10), np.nan, np.float32)
    mask = np.zeros((2, 50), bool)
    mask[0, :2] = True
    mask[1, :1] = True
    actions[mask] = 3.
    stats = prepare.fit_statistics(states, actions, rows, mask)
    assert stats["action"]["mean"] == [3.] * 3
    assert np.isfinite(stats["action"]["std"]).all()


@pytest.mark.parametrize('count,world', [(1,4), (7,4), (95,8), (288,4)])
def test_variable_validation_shards_are_exact_without_padding(count, world):
    from f1_toys.dataset import ExactValidationSampler
    shards = [list(ExactValidationSampler(range(count), world, rank)) for rank in range(world)]
    assert sorted(i for shard in shards for i in shard) == list(range(count))
    assert sum(map(len, shards)) == count


def test_related_corrections_cannot_cross_split():
    split = dict(train=[dict(episode='a', manifest_sha256='a', source_group='session')],
                 validation=[dict(episode='b', manifest_sha256='b', source_group='session')])
    summary = dict(episodes=[dict(episode=x, source_manifest_sha256=x) for x in ('a','b')])
    with pytest.raises(ValueError, match='Related'):
        prepare.validate_split(split, summary)


def test_empty_rank_executes_unscored_collective_forward(monkeypatch):
    monkeypatch.setitem(sys.modules, 'rlinf.utils.utils', SimpleNamespace(get_rng_state=lambda:None, set_rng_state=lambda x:None))
    monkeypatch.setattr(torch.cuda, 'manual_seed_all', lambda *a:None)
    def reduce(value, op):
        if op == torch.distributed.ReduceOp.MAX:
            value.fill_(2)
        else:
            assert value.tolist() == [0., 0.]  # Dummy forwards have no metric weight.
            value.copy_(torch.tensor([10., 5.], dtype=value.dtype))
    monkeypatch.setattr(torch.distributed, 'all_reduce', reduce)
    class Empty:
        dataset = [0]
        collate_fn = staticmethod(lambda x:(None, torch.zeros(1,50,32)))
        def __iter__(self):return iter(())
        def __len__(self):return 0
    forwards = []
    def forward(batch):
        forwards.append(batch); return torch.tensor(999.), {}
    worker = SimpleNamespace(model=torch.nn.Linear(1,1), device='cpu', _rank=3,
        cfg=SimpleNamespace(actor=SimpleNamespace(seed=42)), eval_data_loader=Empty(),
        get_train_model_output=forward)
    assert run_validation(worker) == {'loss':2., 'samples':5}
    assert len(forwards) == 2


@pytest.mark.parametrize("fail", [False, True])
def test_validation_preserves_rng_and_mode_even_on_error(monkeypatch, fail):
    def capture():
        return (torch.get_rng_state(), np.random.get_state(), random.getstate())
    def restore(state):
        torch.set_rng_state(state[0]); np.random.set_state(state[1]); random.setstate(state[2])
    monkeypatch.setitem(sys.modules, "rlinf.utils.utils", SimpleNamespace(get_rng_state=capture, set_rng_state=restore))
    monkeypatch.setattr(torch.cuda, "manual_seed_all", lambda *a: None)
    monkeypatch.setattr(torch.distributed, "all_reduce", lambda *a, **k: None)
    model = torch.nn.Linear(1, 1)
    model.train(not fail)
    def forward(batch):
        value = torch.rand(()) + float(np.random.rand()) + random.random()
        return (value * float("nan") if fail else value), {}
    worker = SimpleNamespace(model=model, device="cpu", _rank=0,
        cfg=SimpleNamespace(actor=SimpleNamespace(seed=42)),
        eval_data_loader=[(None, torch.zeros(2, 50, 32)), (None, torch.zeros(1, 50, 32))],
        get_train_model_output=forward)
    before = capture()
    if fail:
        with pytest.raises(ValueError):
            run_validation(worker)
    else:
        first = run_validation(worker)
        assert first["samples"] == 3
        assert first == run_validation(worker)
    after = capture()
    assert torch.equal(before[0], after[0])
    assert np.array_equal(before[1][1], after[1][1]) and before[1][2:] == after[1][2:]
    assert before[2] == after[2]
    assert model.training == (not fail)


def test_mixed_validation_reports_sources_and_phases_without_reweighting(monkeypatch):
    from torch.utils.data import DataLoader, Dataset, Subset
    monkeypatch.setitem(sys.modules, 'rlinf.utils.utils', SimpleNamespace(
        get_rng_state=lambda: None, set_rng_state=lambda _: None))
    monkeypatch.setattr(torch.cuda, 'manual_seed_all', lambda *a: None)
    monkeypatch.setattr(torch.distributed, 'all_reduce', lambda *a, **k: None)
    class Samples(Dataset):
        manifest = {'sampling': {'method': 'source_episode_anchor_v1'}}
        rows = [dict(dataset_role=role, supervision_phase=phase) for role, phase in
                [('original_demo','human'), ('original_demo','home'),
                 ('correction','release'), ('correction','release'), ('correction','native_home')]]
        def __len__(self): return len(self.rows)
        def __getitem__(self, i): return float(i+1)
    def combine(items):
        return None, torch.tensor(items)
    loader = DataLoader(Subset(Samples(), [0,1,2,3,4]), batch_size=2, collate_fn=combine)
    worker = SimpleNamespace(model=torch.nn.Linear(1,1), device='cpu', _rank=0, _world_size=1,
        cfg=SimpleNamespace(actor=SimpleNamespace(seed=42)), eval_data_loader=loader,
        get_train_model_output=lambda batch: (batch[1].mean(), {}))
    metrics = run_validation(worker)
    assert metrics['loss'] == 3. and metrics['samples'] == 5
    assert metrics['loss/original_demo'] == 1.5 and metrics['samples/original_demo'] == 2
    assert metrics['loss/correction'] == 4. and metrics['samples/correction'] == 3
    assert metrics['loss/correction/release'] == 3.5 and metrics['samples/correction/release'] == 2
    assert metrics['loss/correction/native_home'] == 5.
