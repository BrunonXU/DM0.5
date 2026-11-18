"""Complete CAN windows survive bounded private RPC and content identity checks."""
from copy import deepcopy
import json

import pytest

from f1_harness.contracts import Observation, MeasuredGripperFeedback, HarnessError, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.ipc import MAX_MESSAGE, UnixClient, UnixServer
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.scene_evidence import archive_observation, bind_store, observation_wire
from test_gripper_contact import packet
from test_full_development_scene import full_scene, sample


def measured_observation():
    pipeline, raw = full_scene()
    pipeline.close()
    value = packet()
    source = value['samples'][0]
    value['samples'] = [dict(source, sequence=i+1, received_ns=10_000_000+i*10_000_000,
                             kernel_received_realtime_ns=1_010_000_000+i*10_000_000)
                        for i in range(64)]
    value['latest'] = value['samples'][-1]
    value['received_count'] = 64
    return sample(raw, 7).model_copy(update=dict(host_boot_id='boot',
        measured_gripper=MeasuredGripperFeedback.model_validate(value)))


def archived(tmp_path):
    store = EvidenceStore(tmp_path/'gateway')
    identity = bind_store(tmp_path, store)
    original = measured_observation()
    observation, added = archive_observation(original, store, identity['store_id'])
    return original, observation, added, store


def test_double_observation_and_reverse_proof_restore_all_can_samples(tmp_path):
    original, observation, added, store = archived(tmp_path)
    wire = observation_wire(observation)
    assert 'measured_gripper' not in wire
    assert len(json.dumps(dict(original_observation=wire, observation=wire)).encode()) < MAX_MESSAGE
    calls = []
    def dispatch(method, kwargs):
        calls.append(method)
        before = Observation.model_validate(kwargs['original_observation'])
        latest = Observation.model_validate(kwargs['observation'])
        assert before.measured_gripper == latest.measured_gripper == original.measured_gripper
        return dict(observation=latest.model_dump(mode='json'), observation_sha256=digest(latest))
    server = UnixServer(tmp_path/'plan.sock', dispatch, scene_evidence=True).start()
    try:
        result = UnixClient(server.path, scene_evidence=True).call('revalidate_plan',
            original_observation=wire, observation=wire)
    finally:
        server.close()
    restored = Observation.model_validate(result['observation'])
    assert restored.measured_gripper == original.measured_gripper
    assert len(restored.measured_gripper.samples) == 64
    assert digest(restored) == result['observation_sha256'] == digest(observation)
    assert digest(restored.model_copy(update={'measured_gripper_ref': None})) == digest(original)
    assert calls == ['revalidate_plan']
    assert archive_observation(original, store, observation.measured_gripper_ref.store_id)[1] == 0
    assert added == observation.measured_gripper_ref.byte_count
    # HTTP includes the original independent window for remote summary/trials.
    assert observation_wire(observation, include_measured_gripper=True)['measured_gripper'] == original.measured_gripper.model_dump(mode='json')


@pytest.mark.parametrize('corruption', ['missing', 'bytes', 'size', 'symlink', 'store',
    'observation', 'acquisition', 'host_boot', 'time', 'state_time', 'frame',
    'sensor_boot', 'source_instance', 'first_sequence', 'last_sequence', 'inline'])
def test_invalid_can_reference_never_reaches_consumer(tmp_path, corruption):
    original, observation, _, store = archived(tmp_path)
    wire = deepcopy(observation_wire(observation))
    ref = wire['measured_gripper_ref']
    path = store.root/'blobs'/ref['blob_sha256']
    if corruption == 'missing': path.unlink()
    elif corruption == 'bytes': path.write_bytes(b'X'*ref['byte_count'])
    elif corruption == 'size': ref['byte_count'] += 1
    elif corruption == 'symlink':
        moved=path.with_name('moved');path.rename(moved);path.symlink_to(moved)
    elif corruption == 'store': ref['store_id'] = '0'*64
    elif corruption == 'observation': wire['observation_id'] = 'different'
    elif corruption == 'acquisition': wire['acquisition_instance'] = 'different'
    elif corruption == 'host_boot': wire['host_boot_id'] = 'different'
    elif corruption == 'time': wire['received_ns'] += 1
    elif corruption == 'state_time': wire['state_received_ns'] += 1
    elif corruption == 'frame': wire['frames'][0]['received_ns'] += 1
    elif corruption == 'sensor_boot': ref['sensor_boot_id'] = 'different'
    elif corruption == 'source_instance': ref['source_instance'] = 'different'
    elif corruption == 'first_sequence': ref['first_sequence'] += 1
    elif corruption == 'last_sequence': ref['last_sequence'] += 1
    elif corruption == 'inline': wire['measured_gripper'] = original.measured_gripper.model_dump(mode='json')
    reached=[]
    server=UnixServer(tmp_path/'plan.sock',lambda *args: reached.append(args),scene_evidence=True).start()
    try:
        # Disable outbound packing so the deliberately ambiguous inline input
        # reaches the real receiving boundary and is rejected there.
        with pytest.raises(RuntimeError):
            UnixClient(server.path).call('consume',observation=wire)
        assert not reached
    finally:
        server.close()


def test_joint_depth_can_archive_budget_is_checked_before_any_write(tmp_path):
    from test_scene_evidence_transport import prepared
    _, original, _, _, store, identity = prepared(tmp_path)
    observation=original.model_copy(update={'measured_gripper':measured_observation().measured_gripper})
    quota=len(original.local_collision_evidence.model_dump_json().encode())
    with pytest.raises(HarnessError, match='quota'):
        archive_observation(observation,store,identity['store_id'],remaining_bytes=quota)
    assert not list((store.root/'blobs').iterdir())


@pytest.mark.parametrize('error,code', [
    (ValueError('IPC message too large'),'numerical_rpc_message_too_large'),
    (ValueError('IPC message size rejected'),'numerical_rpc_message_size_rejected'),
    (ConnectionError('incomplete IPC frame'),'numerical_rpc_incomplete_frame'),
])
def test_local_framing_failure_keeps_its_specific_cause(error,code):
    result=numerical_failure_detail(error)
    assert result['code']==code and result['error_type']==type(error).__name__


def test_optional_reference_preserves_old_observation_hash_and_json():
    original=measured_observation()
    value=original.model_dump(mode='json')
    assert 'measured_gripper_ref' not in value
    assert Observation.model_validate(value).model_dump(mode='json')==value
    assert digest(original)==digest(value)
