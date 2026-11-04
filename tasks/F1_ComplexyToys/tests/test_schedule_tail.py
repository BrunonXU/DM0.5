"""Use the real upstream scheduler to check continuity across resume."""
import torch
from transformers.optimization import get_cosine_with_min_lr_schedule_with_warmup

from f1_toys.schedules import hold_schedule_tail


def make(hold=False):
    parameter = torch.nn.Parameter(torch.ones(()))
    optimizer = torch.optim.AdamW([{"params": [parameter], "lr": 1e-4}])
    scheduler = get_cosine_with_min_lr_schedule_with_warmup(
        optimizer, num_warmup_steps=300, num_training_steps=6000,
        num_cycles=0.5, min_lr=1e-5)
    if hold:
        hold_schedule_tail(scheduler, 6000)
    return parameter, optimizer, scheduler


def test_schedule_matches_original_and_holds_floor_without_cosine_rebound():
    _, _, old = make()
    _, _, new = make(True)
    for step in (0, 1, 299, 300, 500, 2000, 4500, 5999, 6000):
        assert new.lr_lambdas[0](step) == old.lr_lambdas[0](step)
    for step in (6001, 10000, 11700, 25000, 50000):
        assert abs(new.lr_lambdas[0](step) * 1e-4 - 1e-6) < 1e-15
    assert old.lr_lambdas[0](11700) > new.lr_lambdas[0](11700)


def test_optimizer_and_scheduler_resume_at_6000_and_again_after_extension():
    p, opt, schedule = make()
    for _ in range(6000):
        p.grad = torch.ones_like(p)
        opt.step(); schedule.step()
    _, restored_opt, restored_schedule = make(True)
    restored_opt.load_state_dict(opt.state_dict())
    restored_schedule.load_state_dict(schedule.state_dict())
    assert restored_schedule.last_epoch == 6000
    assert abs(restored_opt.param_groups[0]['lr'] - 1e-6) < 1e-15
    restored_opt.step(); restored_schedule.step()
    assert restored_schedule.last_epoch == 6001
    assert abs(restored_opt.param_groups[0]['lr'] - 1e-6) < 1e-15
    _, twice_opt, twice_schedule = make(True)
    twice_opt.load_state_dict(restored_opt.state_dict())
    twice_schedule.load_state_dict(restored_schedule.state_dict())
    twice_opt.step(); twice_schedule.step()
    assert twice_schedule.last_epoch == 6002
    assert abs(twice_opt.param_groups[0]['lr'] - 1e-6) < 1e-15
