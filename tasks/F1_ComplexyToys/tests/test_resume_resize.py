"""Real torchdata resume: compare global sample groups across the GPU change."""
import copy

import pytest
from torch.utils.data import DistributedSampler
from torchdata.stateful_dataloader import StatefulDataLoader

from f1_toys.resume import repartition_v1_eight_to_four


def make_loader(world, rank, epoch):
    data = range(11791)
    sampler = DistributedSampler(data, num_replicas=world, rank=rank,
                                 shuffle=True, seed=20261016)
    sampler.set_epoch(epoch)
    return StatefulDataLoader(data, batch_size=2, sampler=sampler,
                              num_workers=0, drop_last=True)


@pytest.mark.parametrize('step', [0, 1, 737, 738, 8000])
def test_remaining_global_batches_and_next_epoch_match(step):
    epoch = (step - 1) // 737 if step else 0
    offset = step - epoch * 737
    old = [make_loader(8, rank, epoch) for rank in range(8)]
    old_iter = [iter(loader) for loader in old]
    for _ in range(offset):
        for iterator in old_iter:
            next(iterator)
    states = [loader.state_dict() for loader in old]
    original = copy.deepcopy(states)
    new = [make_loader(4, rank, epoch) for rank in range(4)]
    for rank, loader in enumerate(new):
        state, new_epoch, new_offset = repartition_v1_eight_to_four(
            states, dict(epoch=epoch, offset=offset), loader, rank=rank,
            step=step, global_batch=16, accumulation=2,
        )
        assert (new_epoch, new_offset) == (epoch, offset * 2)
        loader.load_state_dict(state)
    new_iter = [iter(loader) for loader in new]
    for _ in range(737 - offset):
        old_ids = sorted(v for iterator in old_iter for v in next(iterator).tolist())
        new_ids = sorted(v for _ in range(2) for iterator in new_iter
                         for v in next(iterator).tolist())
        assert new_ids == old_ids
    for iterator in old_iter + new_iter:
        with pytest.raises(StopIteration):
            next(iterator)
    for loader in old + new:
        loader.sampler.set_epoch(epoch + 1)
    old_iter, new_iter = [iter(x) for x in old], [iter(x) for x in new]
    assert sorted(v for it in old_iter for v in next(it).tolist()) == sorted(
        v for _ in range(2) for it in new_iter for v in next(it).tolist()
    )
    assert states == original


def test_reject_changed_recipe_or_corrupted_rank_cursor():
    old = [make_loader(8, r, 0) for r in range(8)]
    states = [x.state_dict() for x in old]
    loader = make_loader(4, 0, 0)
    arguments = dict(rank=0, step=0, global_batch=16, accumulation=2)
    with pytest.raises(ValueError, match='geometry'):
        repartition_v1_eight_to_four(states, dict(epoch=0, offset=0), loader,
                                   **(arguments | {'global_batch': 8}))
    states[7]['_num_yielded'] = 1
    with pytest.raises(ValueError, match='inconsistent'):
        repartition_v1_eight_to_four(states, dict(epoch=0, offset=0), loader, **arguments)
