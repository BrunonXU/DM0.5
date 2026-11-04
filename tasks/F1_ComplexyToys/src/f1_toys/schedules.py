"""Continue an exhausted learning-rate schedule at its final value."""
from functools import partial


def _at_or_before_end(step, *, schedule, end):
    return schedule(min(step, end))


def hold_schedule_tail(scheduler, end):
    if end <= 0:
        raise ValueError("Schedule end must be positive")
    scheduler.lr_lambdas = [partial(_at_or_before_end, schedule=f, end=end)
                            for f in scheduler.lr_lambdas]
    return scheduler
