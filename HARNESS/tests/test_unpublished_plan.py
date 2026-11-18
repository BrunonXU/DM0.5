from concurrent.futures import Future

import pytest

from f1_harness.contracts import Frame, Observation, SkillIntent, Task, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.owner_bridge import JointProposal, OwnerBridge


class ControlledExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = Future(); self.jobs.append((future, function, args))
        return future

    def shutdown(self, **kwargs):
        pass


def make_bridge(tmp_path):
    clock = [10_000_000_000]
    engine = TaskEngine(EvidenceStore(tmp_path), mode='hardware', clock=lambda: clock[0], lease_ms=20000)
    session = engine.open_session()['session_id']
    engine.prepare_task(session, Task(task_id='single', object_id='block', hole_id='hole', calibration_id='real').model_dump())
    engine.hardware_ready = True
    engine.local_start(physical_event=True, feedback_healthy=True)
    frame = Frame(camera='right', stream='rgb', frame_id='right', sequence=1,
        source_ns=clock[0], received_ns=clock[0], clock_domain='original_source',
        width=2, height=2, encoding='rgb8', blob_sha256='0'*64)
    observation = Observation(observation_id='original', acquisition_instance='camera',
        received_ns=clock[0], clock_domain='owner_boot', host_boot_id=engine.host_boot_id,
        state16=(0.,)*15+(18.,), state_valid_mask=(True,)*16, state_received_ns=clock[0],
        frames=(frame,), scene_version='scene', calibration_id='real', stationary=True)
    engine.update_observation(observation)
    intent = SkillIntent(request_id='one-request', session_id=session, owner_instance_id=engine.instance,
        task_id='single', task_hash=digest(engine.task), epoch=engine.epoch,
        observation_id='original', scene_version='scene', calibration_id='real', skill='approach_grasp')
    engine.submit_skill(intent.model_dump())
    bridge = OwnerBridge(engine, numerical_planner=lambda *args: None, result_verifier=lambda *args: None,
        clock=lambda: clock[0], feedback_age_ns=200_000_000, reference_tolerance_deg=.5,
        stop_gripper_delta=.1)
    bridge.executor.shutdown()
    bridge.executor = ControlledExecutor()
    state = dict(full22=[0.]*22, measured16=[0.]*15+[18.], held16=[0.]*15+[18.])
    def poll():
        return bridge.poll(**state, stamps16=(clock[0],)*16)
    return engine, bridge, clock, state, poll, observation


def finish_plan(bridge):
    future, _, (intent, observation, full, held) = bridge.executor.jobs[-1]
    target = list(held); target[7] += .1
    proposal = JointProposal(intent.request_id, intent.epoch, intent.scene_version, intent.calibration_id,
        full, (tuple(target),), .1, 100_000_000, .5)
    future.set_result(proposal)
    return proposal


def test_fresh_snapshot_can_start_and_own_motion_does_not_look_like_planning_drift(tmp_path):
    engine, bridge, clock, state, poll, _ = make_bridge(tmp_path)
    try:
        assert poll() is None
        finish_plan(bridge)
        target = poll()
        assert target[7] == .1
        bridge.after_publication(publication_id='first', measured16=state['measured16'],
                                 publication_complete=True, now_ns=clock[0])
        assert bridge.proposal_started
        clock[0] += 600_000_000
        state['full22'][7] = state['measured16'][7] = .1
        assert poll()[7] == .1 and engine.authorized
    finally:
        bridge.close()


def test_eight_second_ik_is_discarded_even_if_new_observation_arrives(tmp_path):
    engine, bridge, clock, _, poll, original = make_bridge(tmp_path)
    try:
        assert poll() is None
        clock[0] += 8_000_000_000
        engine.update_observation(original.model_copy(update={'observation_id':'new', 'received_ns':clock[0]}))
        finish_plan(bridge)
        assert poll() is None
        assert engine.results['one-request'].reason == 'planning_observation_expired_before_publication'
        result=engine.results['one-request']
        assert result.outcome == 'failure' and result.no_motion and result.retryable
        assert not engine.hold_requested and engine.authorized
        assert bridge.current_target is None
        assert len(bridge.executor.jobs) == 1
    finally:
        bridge.close()


def test_new_observation_does_not_replace_a_still_fresh_plan_snapshot(tmp_path):
    engine, bridge, clock, _, poll, original = make_bridge(tmp_path)
    try:
        assert poll() is None
        clock[0] += 20_000_000
        engine.update_observation(original.model_copy(update={'observation_id':'new', 'received_ns':clock[0]}))
        finish_plan(bridge)
        assert poll() is not None and bridge.plan_observation.observation_id == 'original'
    finally:
        bridge.close()


def test_overwritten_observation_before_dispatch_never_enters_numerics(tmp_path):
    engine, bridge, _, _, poll, original = make_bridge(tmp_path)
    try:
        engine.update_observation(original.model_copy(update={'observation_id':'replaced'}))
        assert poll() is None and not bridge.executor.jobs
        assert engine.results['one-request'].reason == 'planning_observation_changed_before_dispatch'
    finally:
        bridge.close()


def test_old_source_frame_is_not_refreshed_by_observation_timestamp(tmp_path):
    engine, bridge, clock, _, poll, original = make_bridge(tmp_path)
    frame = original.frames[0].model_copy(update={'received_ns':clock[0]-490_000_000})
    engine.update_observation(original.model_copy(update={'frames':(frame,)}))
    try:
        assert poll() is None
        clock[0] += 20_000_000; finish_plan(bridge)
        assert poll() is None
        assert engine.results['one-request'].reason == 'planning_frame_expired_before_publication'
    finally:
        bridge.close()


@pytest.mark.parametrize('field,index,value,reason', [
    ('full22', 7, .6, 'right_arm_reference_changed_during_planning'),
    ('measured16', 7, .6, 'right_arm_reference_changed_during_planning'),
    ('held16', 7, .6, 'right_arm_reference_changed_during_planning'),
    ('measured16', 15, 19., 'gripper_reference_changed_during_planning'),
    ('held16', 15, 19., 'gripper_reference_changed_during_planning'),
    ('full22', 14, .6, 'body_or_head_reference_changed'),
])
def test_feedback_and_command_drift_cannot_start_old_plan(tmp_path, field, index, value, reason):
    engine, bridge, _, state, poll, _ = make_bridge(tmp_path)
    try:
        assert poll() is None; finish_plan(bridge)
        state[field][index] = value
        assert poll() is None
        assert engine.results['one-request'].reason == reason
        assert bridge.proposal is None and engine.hold_requested
    finally:
        bridge.close()


def test_publication_hook_catches_expiry_after_candidate_was_selected(tmp_path):
    engine, bridge, clock, state, poll, _ = make_bridge(tmp_path)
    try:
        assert poll() is None; finish_plan(bridge)
        clock[0] += 499_000_000
        assert poll() is not None
        clock[0] += 2_000_000
        assert not bridge.validate_first_publication(**state, stamps16=(clock[0],)*16, now_ns=clock[0])
        assert not engine.hold_requested and engine.authorized and bridge.current_target is None
        assert engine.results['one-request'].no_motion and engine.results['one-request'].retryable
    finally:
        bridge.close()


def test_proposal_cannot_replace_original_reference_with_current_state(tmp_path):
    engine, bridge, _, _, poll, _ = make_bridge(tmp_path)
    try:
        assert poll() is None
        future, _, (intent, _, full, held) = bridge.executor.jobs[-1]
        changed = list(full); changed[7] = .2
        future.set_result(JointProposal(intent.request_id, intent.epoch, intent.scene_version,
            intent.calibration_id, tuple(changed), (held,), .1, 1, .5))
        assert poll() is None
        assert engine.results['one-request'].reason == 'numerical_reference_differs_from_planning_snapshot'
    finally:
        bridge.close()


def test_publication_hook_rechecks_measured_development_opening_and_stamp(tmp_path):
    engine, bridge, clock, state, poll, _ = make_bridge(tmp_path)
    engine.development_admission = {'goal':'approach_grasp','right_reference': {'reference_gripper_closure': 18}}
    try:
        assert poll() is None; finish_plan(bridge)
        assert poll() is not None
        # Commands still say 18. Measured 18.2 exceeds the existing fixed-reference
        # tolerance, though it is within the proposal's broader arrival tolerance.
        state['measured16'][15] = 18.2
        assert not bridge.validate_first_publication(**state, stamps16=(clock[0],)*16, now_ns=clock[0])
        assert engine.results['one-request'].reason == 'measured_development_gripper_reference_changed'
    finally:
        bridge.close()


def test_stale_gripper_feedback_rejected_before_first_publication(tmp_path):
    engine, bridge, clock, state, poll, _ = make_bridge(tmp_path)
    try:
        assert poll() is None; finish_plan(bridge)
        assert poll() is not None
        stamps = [clock[0]]*16; stamps[15] -= 201_000_000
        assert not bridge.validate_first_publication(**state, stamps16=stamps, now_ns=clock[0])
        assert engine.results['one-request'].reason == 'planning_feedback_stale_before_publication'
    finally:
        bridge.close()
