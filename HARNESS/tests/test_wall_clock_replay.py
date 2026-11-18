"""Offline scheduling fixtures must not freeze time or share planner state."""
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]/'scripts'))
from wall_clock_replay import PacedClock, AsyncReplayExecutor
from f1_harness.contracts import SkillIntent


def test_pacer_counts_actual_lateness_and_never_catches_up_with_zero_dt(monkeypatch):
    now=[1_000_000_000]
    def sleep(seconds):now[0]+=round(seconds*1e9)
    monkeypatch.setattr(time,'monotonic_ns',lambda:now[0])
    monkeypatch.setattr(time,'sleep',sleep)
    paced=PacedClock(origin_ns=10_000_000_000,max_seconds=1)
    assert paced.advance()==10_020_000_000
    now[0]+=45_000_000
    assert paced.advance()==10_065_000_000
    assert paced.advance()==10_085_000_000
    assert paced.intervals_ns==[20_000_000,45_000_000,20_000_000]
    now[0]+=1_000_000_000
    with pytest.raises(TimeoutError,match='offline_wall_clock_run_budget'):
        paced.advance()


def test_control_can_pump_while_worker_is_running_and_cancel_queued_work():
    records=[];executor=AsyncReplayExecutor(records)
    entered=threading.Event();release=threading.Event()
    intent=SkillIntent.model_construct(request_id='r',skill='approach_grasp')
    def plan(intent):
        entered.set()
        assert release.wait(2)
        return intent.request_id
    try:
        running=executor.submit(plan,intent)
        assert entered.wait(1)
        executor.pump()
        assert executor.active_jobs()==[dict(request_id='r',function='plan')]
        queued=executor.submit(plan,intent)
        assert queued.cancel()
        assert not running.cancel()
        assert not running.done()
        release.set()
        assert running.result(timeout=1)=='r'
    finally:
        release.set();executor.shutdown(wait=True,cancel_futures=True)
    assert not executor.active_jobs()
    assert len(records)==1 and records[0]['error_type'] is None
    assert records[0]['queued_wall_ns']<=records[0]['started_wall_ns']<=records[0]['ended_wall_ns']


def test_worker_failure_is_preserved_and_not_replaced_by_success():
    records=[];executor=AsyncReplayExecutor(records)
    intent=SkillIntent.model_construct(request_id='r',skill='align')
    def failing(intent):raise ValueError('native_failure')
    try:
        with pytest.raises(ValueError,match='native_failure'):
            executor.submit(failing,intent).result(timeout=1)
    finally:executor.shutdown(wait=True,cancel_futures=True)
    assert records[0]['error_type']=='ValueError'
    assert executor.active_jobs()==[]
