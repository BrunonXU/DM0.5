from types import SimpleNamespace
import time
import numpy as np
import pytest

from f1dagger.manual_compute import ManualPlanner
from f1dagger.faults import ManualReferenceChanged


class SlowNumericalFake:
    def __init__(self, spec): self.delay = spec['delay']
    def __call__(self, request):
        time.sleep(self.delay)
        target = list(request['held']); target[7] += request['axes'][0]
        return dict(candidate=target, selected=target, shortened=None)


def packet(x=1.):
    return SimpleNamespace(session_id='session', active_side='right', mode='translation',
                           axes=(x,0.,0.), angular_axes=(0.,0.,0.), speed_scale=1.)


def worker(delay=.03, max_age=.15):
    return ManualPlanner(dict(delay=delay), max_age_s=max_age,
                         factory=__name__+':SlowNumericalFake')


def test_slow_compute_does_not_block_control_and_has_no_backlog():
    w=worker()
    try:
        full, held=np.zeros(22),np.zeros(16)
        started=time.monotonic()
        assert w.poll(packet(),full,held,.02,1) is None
        assert time.monotonic()-started < .02
        assert w.sequence==1
        for _ in range(3):
            assert w.poll(packet(),full,held,.02,1) is None
        assert w.sequence==1
        time.sleep(.04)
        result=w.poll(packet(),full,held,.02,1)
        assert result['selected'][7] == 1.
        assert w.continuing(packet())
        assert w.sequence==2
    finally: w.close()


@pytest.mark.parametrize('change', ['release', 'epoch', 'direction', 'session'])
def test_late_result_cannot_regain_control(change):
    w=worker()
    try:
        full,held=np.zeros(22),np.zeros(16)
        p=packet(); epoch=1
        w.poll(p,full,held,.02,epoch)
        if change=='release': w.invalidate()
        if change=='epoch': epoch+=1
        if change=='direction': p=packet(-1.)
        if change=='session': p.session_id='new-session'
        time.sleep(.04)
        assert w.poll(p,full,held,.02,epoch) is None
        assert not w.continuing(p)
    finally: w.close()


def test_hung_worker_expires_only_manual_candidate():
    w=worker(.4, .08)
    try:
        full,held=np.zeros(22),np.zeros(16)
        w.poll(packet(),full,held,.02,1)
        time.sleep(.09)
        started=time.monotonic()
        with pytest.raises(ManualReferenceChanged,match='target lifetime'):
            w.poll(packet(),full,held,.02,1)
        assert time.monotonic()-started < .02
        assert not w.continuing(packet())
    finally:w.close()


def test_control_publishes_hold_while_computing_and_release_revokes():
    from test_f1_runtime import setup_loop
    loop,e,o,t,now,records,step=setup_loop()
    class Fake:
        accepted=False
        generation=0
        def invalidate(self): self.generation+=1;self.accepted=False
        def poll(self,*args): return None
        def continuing(self,p): return self.accepted
    planner=Fake();loop.manual_planner=planner
    try:
        step(0);step(1,1)
        previous=o.last_submitted.copy()
        step(2,1,deadman=True)
        assert records[-1]['source']=='human_compute_wait'
        np.testing.assert_equal(o.last_submitted,previous)
        assert t.sent  # Control publication still runs while work is pending.
        generation=planner.generation
        step(3,1,deadman=False)
        assert planner.generation>generation
        assert not planner.accepted
    finally:e.close()


def test_continuous_stick_magnitude_does_not_starve_pipeline():
    w=worker()
    try:
        full,held=np.zeros(22),np.zeros(16)
        w.poll(packet(.5),full,held,.02,1)
        time.sleep(.04)
        reply=w.poll(packet(.51),full,held,.02,1)
        assert reply['selected'][7]==.5
        assert reply['input_axes'][0]==.5
        assert w.continuing(packet(.51))
        assert not w.continuing(packet(-.51))
        assert not w.continuing(packet(0.))
    finally:w.close()


def test_body_reference_changed_during_worker_request_is_not_adopted():
    w=worker()
    try:
        full,held=np.zeros(22),np.zeros(16)
        w.poll(packet(),full,held,.02,1)
        time.sleep(.04)
        full[-1]=.6
        with pytest.raises(ManualReferenceChanged,match='reference changed'):
            w.poll(packet(),full,held,.02,1)
        assert not w.continuing(packet())
    finally:w.close()


def test_worker_disconnect_during_send_is_recoverable_manual_fault(monkeypatch):
    w=worker()
    try:
        def broken(_): raise BrokenPipeError('worker exited between alive check and send')
        monkeypatch.setattr(w.connection,'send',broken)
        with pytest.raises(ManualReferenceChanged,match='disconnected during request'):
            w.poll(packet(),np.zeros(22),np.zeros(16),.02,1)
        assert w.pending is None and not w.continuing(packet())
    finally:w.close()


def test_long_reference_interval_reanchors_without_integrating_wait():
    w = worker(.01)
    try:
        w.poll(packet(), np.zeros(22), np.zeros(16), .004, 1)
        generation = w.generation
        time.sleep(.07)
        assert w.poll(packet(), np.zeros(22), np.zeros(16), .004, 1) is None
        assert w.generation > generation and w.pending is not None
        assert not w.continuing(packet())
        time.sleep(.02)
        assert w.poll(packet(), np.zeros(22), np.zeros(16), .004, 1) is not None
    finally:
        w.close()
