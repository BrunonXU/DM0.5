from collections import Counter
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.sampling import SourceEpisodeSampler


def rows():
    return [dict(dataset_role=role,episode_id=ep) for role,ep,n in
            [('original_demo','a',50),('original_demo','b',150),('correction','c',15),('correction','d',350)]
            for _ in range(n)]


def sampler(rank=0,world=4,epoch=0):
    s=SourceEpisodeSampler(rows(),counts={'original_demo':96,'correction':32},global_batch=128,
                           num_replicas=world,rank=rank,seed=20261021)
    s.set_epoch(epoch);return s


def test_every_update_has_exact_quota_and_rank_independent_global_sequence():
    data=rows();shards=[list(sampler(r)) for r in range(4)]
    merged=[shards[r][j] for j in range(len(shards[0])) for r in range(4)]
    assert merged==list(sampler(world=1))
    for offset in range(0,len(merged),128):
        assert Counter(data[i]['dataset_role'] for i in merged[offset:offset+128])=={'original_demo':96,'correction':32}
    assert merged!=list(sampler(world=1,epoch=1))


def test_episode_selection_not_weighted_by_recording_length_and_rng_isolated():
    np.random.seed(17);before=np.random.get_state()
    counts=Counter()
    for epoch in range(100):
        counts.update(rows()[i]['episode_id'] for i in sampler(world=1,epoch=epoch))
    for a,b in [('a','b'),('c','d')]:assert .9<counts[a]/counts[b]<1.1
    after=np.random.get_state()
    assert before[0]==after[0] and before[2:]==after[2:]
    np.testing.assert_array_equal(before[1],after[1])


def test_stateful_loader_resume_mid_epoch_and_at_epoch_boundary():
    mod=pytest.importorskip('torchdata.stateful_dataloader')
    import torch
    dataset=torch.arange(len(rows()))
    make=lambda:mod.StatefulDataLoader(dataset,batch_size=2,sampler=sampler(),num_workers=0,drop_last=True)
    first=make();it=iter(first)
    for _ in range(32):next(it)
    state=first.state_dict();expected=[x.tolist() for x in it]
    restored=make();restored.load_state_dict(state)
    assert [x.tolist() for x in restored]==expected
    first.sampler.set_epoch(1);restored.sampler.set_epoch(1)
    assert [x.tolist() for x in first]==[x.tolist() for x in restored]


def test_resume_preserves_augmentation_draws_and_training_torch_rng():
    mod=pytest.importorskip('torchdata.stateful_dataloader')
    import torch
    class Augmented(torch.utils.data.Dataset):
        def __len__(self): return len(rows())
        def __getitem__(self,i):
            return i, int(np.random.randint(0,2**32))
    make=lambda:mod.StatefulDataLoader(Augmented(),batch_size=2,sampler=sampler(),num_workers=0)
    np.random.seed(12);torch.manual_seed(12)
    first=make();it=iter(first)
    for _ in range(32):next(it)
    saved=first.state_dict();numpy_rng=np.random.get_state();torch_rng=torch.get_rng_state()
    expected=[(a.tolist(),b.tolist(),torch.randn(2).tolist()) for a,b in it]
    # Matches worker order: restore epoch/cursor, instantiate iterator, then RNG.
    restored=make();restored.load_state_dict(saved);replayed=iter(restored)
    np.random.set_state(numpy_rng);torch.set_rng_state(torch_rng)
    actual=[(a.tolist(),b.tolist(),torch.randn(2).tolist()) for a,b in replayed]
    assert actual==expected


def test_unknown_source_and_invalid_quota_rejected():
    with pytest.raises(ValueError):
        SourceEpisodeSampler(rows(),counts={'original_demo':128},global_batch=128,num_replicas=4,rank=0,seed=1)


def focused(multiplier, rank=0, world=1, epoch=0):
    data = rows()
    focus = [i for i in range(len(data)) if i % 5 == 0]
    s = SourceEpisodeSampler(data, counts={'original_demo':96, 'correction':32},
        global_batch=128, num_replicas=world, rank=rank, seed=20261021,
        focus_indices=focus, focus_multiplier=multiplier)
    s.set_epoch(epoch)
    return s


def test_focus_preserves_legacy_stream_source_episode_order_and_rank_sharding():
    data = rows()
    assert list(focused(1)) == list(sampler(world=1))
    baseline, weighted = list(focused(1)), list(focused(4))
    assert baseline != weighted
    assert [data[i] for i in baseline] == [data[i] for i in weighted]
    shards = [list(focused(4, rank=r, world=4)) for r in range(4)]
    assert weighted == [shards[r][j] for j in range(len(shards[0])) for r in range(4)]


def test_focus_odds_change_without_removing_any_other_anchor():
    # Each episode is exactly 20% focus: fourfold odds gives 50% draws.
    selected = [i for epoch in range(60) for i in focused(4, epoch=epoch)]
    assert .48 < np.mean([i % 5 == 0 for i in selected]) < .52
    assert set(selected) == set(range(len(rows())))


def test_weighted_resume_and_fork_preserve_cursor_and_epoch():
    mod = pytest.importorskip('torchdata.stateful_dataloader')
    import torch
    def make(multiplier):
        return mod.StatefulDataLoader(torch.arange(len(rows())), batch_size=2,
            sampler=focused(multiplier, world=4, epoch=7), num_workers=0)
    for parent_multiplier in (1, 4):
        parent = make(parent_multiplier)
        it = iter(parent)
        for _ in range(32): next(it)
        state = parent.state_dict()
        expected = [x.tolist() for x in make(4)][32:]
        child = make(4)
        child.load_state_dict(state)
        assert [x.tolist() for x in child] == expected
        child.sampler.set_epoch(8)
        reference = make(4)
        reference.sampler.set_epoch(8)
        assert [x.tolist() for x in child] == [x.tolist() for x in reference]


@pytest.mark.parametrize('indices,multiplier', [([],4),([0,0],4),([-1],4),([0],float('nan')),([0],.5)])
def test_invalid_focus_rejected(indices, multiplier):
    with pytest.raises(ValueError):
        SourceEpisodeSampler(rows(), counts={'original_demo':96,'correction':32},
            global_batch=128, num_replicas=4, rank=0, seed=1,
            focus_indices=indices, focus_multiplier=multiplier)
