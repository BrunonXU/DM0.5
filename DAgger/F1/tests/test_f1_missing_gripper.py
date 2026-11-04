from dataclasses import replace
import json
import numpy as np
import pytest
from f1dagger.hardware import LEFT_GRIPPER_REMOVED, state_mask
from f1dagger.observation import RosObservationSource
from f1dagger.recording import F1Recorder, make_sink, audit
from test_f1_observation import store
from test_f1_output import make_owner
from test_f1_policy import observation


CFG = dict(hardware_capabilities=LEFT_GRIPPER_REMOVED)
MASK = state_mask(CFG)


def test_absent_left_is_not_driver_cached_feedback():
    s = store()
    s.grippers['left']['position'] = 99
    source = RosObservationSource(s, {'feedback_max_age_s': .25}, CFG)
    _, state, stamps = source.feedback()
    assert state[14] == stamps[14] == 0
    del s.grippers['left']
    assert source.feedback()[1][14] == 0
    del s.grippers['right']
    with pytest.raises(RuntimeError, match='missing/stale'):
        source.feedback()


def test_absent_left_does_not_relax_full_body_checks():
    s = store(); del s.joints['head_j2']
    with pytest.raises(RuntimeError, match='missing/stale'):
        RosObservationSource(s, {'feedback_max_age_s': .25}, CFG).feedback()


def test_hardware_profile_fails_closed():
    assert state_mask({}) == (True,) * 16
    for change in ({'right_gripper_present': False}, {'left_gripper_present': 0}, {'left_wrist_camera_present': False}):
        with pytest.raises(ValueError):
            state_mask(dict(hardware_capabilities=dict(LEFT_GRIPPER_REMOVED, **change)))


def test_no_left_publisher_or_fabricated_publication_timestamp():
    owner, transport = make_owner()
    from f1dagger.robot_io import F1Owner
    owner.source.state_valid_mask = MASK
    state = np.r_[np.zeros(14), 0., 66.]
    owner.source.feedback = lambda: (np.zeros(22), state, (1,) * 14 + (0, 1))
    owner = F1Owner(transport, owner.source, owner.mapping, owner.output, owner.gate, powered=True)
    owner.initialize(1.); owner.prepare()
    command, _, evidence = owner.publish(1.01)
    assert transport.prepared == ['joint_command', 'right_gripper_command']
    assert [k for k, _ in transport.sent] == transport.prepared
    assert command[14] == 0 and evidence['action_source_timestamps_ns'][14] is None
    assert evidence['action_valid_mask'] == list(MASK)
    transport.conflict = True
    with pytest.raises(RuntimeError, match='external command'):
        owner.publish(1.02)


def test_v4_recording_roundtrip_marks_missing_hardware(tmp_path):
    run = tmp_path/'raw'; run.mkdir()
    binding = dict(CFG, state_valid_mask=list(MASK), execution_mode='powered', recording_mode='hil-record')
    recorder = F1Recorder(run/'journal', episode_id='synthetic-missing-left', task_prompt='test', binding=binding)
    sink = make_sink(run/'payload', 'hil-record')
    obs = observation(0, 1_000_000_000)
    obs = replace(obs, state16=tuple(list(obs.state16[:14])+[0, obs.state16[15]]),
                  state_timestamps_ns=obs.state_timestamps_ns[:14]+(0, obs.state_timestamps_ns[15]), state_valid_mask=MASK)
    assert sink.submit(obs)
    recorder.record_timestamped_observation(obs, hil_state='hil_latched', intervention_id=1, policy_epoch=1)
    t = obs.available_monotonic_ns+1
    stamps = [t-1]*14+[None, t-1]
    recorder.control(now=t, state='hil_latched', intervention=1, epoch=1, selected=np.zeros(16),
        submitted=np.zeros(16), source='human_arm', measured=obs.state16, sequence=1,
        publication_complete=True, execution_mode='powered', action_source_timestamps_ns=stamps,
        state_valid_mask=list(MASK), action_valid_mask=list(MASK),
        publications=[dict(topic_key=k, monotonic_ns=t-1, publisher_gid='aa'*16)
                      for k in ['joint_command', 'right_gripper_command']])
    assert sink.close() and recorder.close()
    assert json.loads((run/'journal/manifest.json').read_text())['schema_version'] == 4
    result = audit(run)
    assert result['valid'], result
    assert not result['training_ready']
    from f1dagger.export import select_segments
    from f1dagger.contract import CONTRACT
    review = dict(contract=CONTRACT, manifest_sha256=result['manifest_sha256'], reviewer='test', accepted_interventions=[1])
    with pytest.raises(ValueError, match='no reviewed, contiguous'):
        select_segments(run, review)
    with pytest.raises(ValueError, match='timestamp'):
        replace(obs, state_valid_mask=None)


def test_control_boundary_requires_all_present_feedback():
    from types import SimpleNamespace
    from f1dagger.policy import F1PolicyEngine
    obs = observation(0, 1_000_000_000)
    stamps = obs.state_timestamps_ns
    obs = replace(obs, state16=tuple(list(obs.state16[:14])+[0, obs.state16[15]]),
                  state_timestamps_ns=stamps[:14]+(0, stamps[15]), state_valid_mask=MASK)
    engine = SimpleNamespace(minimum_observation_ns=min(stamps)-1)
    assert F1PolicyEngine.policy_input_issue(engine, obs) is None
    for i in list(range(14))+[15]:
        old = list(obs.state_timestamps_ns); old[i] = engine.minimum_observation_ns
        assert F1PolicyEngine.policy_input_issue(engine, replace(obs, state_timestamps_ns=tuple(old))) == 'state_not_received_after_control_boundary'
