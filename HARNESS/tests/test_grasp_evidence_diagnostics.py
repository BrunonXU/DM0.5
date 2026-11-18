from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.live_scene import LiveSceneObserver
from test_scene_pipeline import fixture


def diagnostic_observer():
    _, obs = fixture()
    evidence = dict(camera='head', received_ns=1, source_ns=[1, 1], held=None,
                    reason='insufficient_independent_following_samples')
    sample = dict(time=1, obj=np.eye(4), eef=np.eye(4), relative=np.eye(4), gripper=100.,joints=np.zeros(7))
    scene = LiveSceneObserver.__new__(LiveSceneObserver)
    scene.pipeline = SimpleNamespace(last_timing={'primary_evidence': evidence}, stationary_history=[sample])
    scene.last_timing = {}
    events = []
    scene.store = SimpleNamespace(event=lambda name, **kwargs: events.append((name, kwargs)))
    return scene, obs, events


def test_grasp_diagnostics_preserve_per_frame_measurements_without_duplicate_poll_records():
    scene, obs, events = diagnostic_observer()
    context = dict(phase='grasp', epoch=1)
    scene._record_grasp_evidence(context, obs)
    scene._record_grasp_evidence(context, obs)
    assert len(events) == 1
    name, row = events[0]
    assert name == 'grasp_evidence_sample' and row['diagnostic_only'] is True
    assert row['measurements']['held'] is None and row['gripper'] == 100.
    assert row['relative_pose'] == np.eye(4).tolist()
    assert len(row['frames']) == 2
    assert row['context'] == context


@pytest.mark.parametrize('fault', ['storage', 'incomplete_sample'])
def test_optional_diagnostic_failure_cannot_invalidate_scene(fault):
    scene, obs, _ = diagnostic_observer()
    if fault == 'storage':
        scene.store.event = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('unavailable'))
    else:
        scene.pipeline.stationary_history[-1].pop('relative')
    scene._record_grasp_evidence({'phase': 'grasp'}, obs)
    assert 'grasp_diagnostic_error' in scene.last_timing
    assert not hasattr(scene, 'last_grasp_diagnostic_key')


def test_old_measurement_and_unrelated_phase_are_not_logged_as_grasp_evidence():
    scene, obs, events = diagnostic_observer()
    scene._record_grasp_evidence({'phase': 'observe'}, obs)
    scene.pipeline.stationary_history[-1]['time'] = 2
    scene._record_grasp_evidence({'phase': 'grasp'}, obs)
    assert not events
