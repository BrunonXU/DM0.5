"""Processing time consumes the sample interval; it does not renew evidence."""
from types import SimpleNamespace
import pytest
from f1_harness.live_scene import LiveSceneObserver


@pytest.mark.parametrize('work_s,expected_delay',[(.25,.001),(.02,.08)])
def test_processing_does_not_add_another_full_poll_interval(monkeypatch,work_s,expected_delay):
    clock=[0.];delays=[];samples=[]
    monkeypatch.setattr('f1_harness.live_scene.time.perf_counter',lambda:clock[0])
    class Stop:
        def is_set(self):return len(delays)==3
        def wait(self,delay):delays.append(delay);clock[0]+=delay
    observer=LiveSceneObserver.__new__(LiveSceneObserver)
    observer.stop=Stop();observer.config={'poll_seconds':.1};observer.error=None
    observer.store=SimpleNamespace(event=lambda *a,**kw:None)
    def sample():samples.append(clock[0]);clock[0]+=work_s
    observer._sample=sample
    observer._run()
    assert delays==pytest.approx([expected_delay]*3)
    assert samples[1]-samples[0]==pytest.approx(work_s+expected_delay)


def test_coarse_processing_over_budget_is_published_with_original_times(monkeypatch):
    from test_phase_live_scene import observer
    scene,verified=observer(monkeypatch)
    raw=scene.raw.observe()
    scene.config['geometry']={'camera':raw.frames[0].camera,'admission_profile':'development_coarse'}
    scene.pipeline.process=lambda value,**kw:value.model_copy(update={'geometry_scope':'coarse_static_task'})
    ticks=iter([1,600_000_001,600_000_002,600_000_003])
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:next(ticks))
    scene._sample()
    assert len(verified)==1 and len(scene.ingested)==1
    assert scene.latest.frames==raw.frames
    assert scene.latest.received_ns==raw.received_ns
    assert 'visual_measurements_above_age_budget' in scene.latest.quality_warnings
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:3_000_000_001)
    assert scene.observe().frames==raw.frames


def test_coarse_quality_policy_still_rejects_future_timestamps(monkeypatch):
    scene=LiveSceneObserver.__new__(LiveSceneObserver)
    scene.config={'max_age_ms':500,'geometry':{'admission_profile':'development_coarse'}}
    scene.last_timing={}
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:1)
    with pytest.raises(ValueError,match='scene_clock_in_future'):
        scene._ensure_fresh('perception_latency',[2])
