from types import SimpleNamespace

import pytest

from f1_harness.ipc import GEOMETRY_HTTP_TIMEOUT_S, GEOMETRY_OWNER_TIMEOUT_S, GEOMETRY_NUMERICAL_TIMEOUT_S
from f1_harness.program_experience import uses_only_target_presence, validate_candidate
from f1_harness.remote import OwnerProxy, RemoteTaskBackend


@pytest.mark.parametrize(('expression','supported'),[
    ('scene.get("available") is True or scene.get("object_pose") is not None',True),
    ('not (scene.get("object_pose") is None)',True),
    ('scene.get("object_pose")[3] > .2',False),
    ('scene.get("grasp_suggestions") is not None',False),
    ('bool(scene)',False),
    ('scene.get("available", True) is True',False),
])
def test_only_pose_existence_predicates_can_use_the_projection(expression,supported):
    candidate=validate_candidate(dict(title='predicate',claim='test',
        source='def applicable(scene):\n    return '+expression+'\n\ndef run_skill(robo):\n    return robo.geometry()'))
    assert uses_only_target_presence(candidate) is supported


def test_projected_and_full_geometry_follow_separate_read_paths(tmp_path):
    from f1_harness.contracts import HarnessError
    remote=object.__new__(RemoteTaskBackend)
    remote.pending=None
    remote.task=SimpleNamespace(object_id='block')
    object_pose=[1.]*16
    obj=SimpleNamespace(object_id='block',pose=object_pose,frame_id='registered-world')
    observation=SimpleNamespace(objects=[obj],observation_id='sample',calibration_id='calib',
        received_ns=123,geometry_scope='coarse_static_task')
    remote._await_observation=lambda **kwargs:(observation,dict(mode='hardware'))
    remote.session='session'
    calls=[]
    def owner_geometry(method,**kwargs):
        calls.append((method,kwargs))
        return dict(object_pose=object_pose,grasp_suggestions={'candidates':[1]},obstacles=[])
    remote.call=owner_geometry
    bounded=remote.read_program_target_state()
    assert not calls and bounded['object_pose']==object_pose and bounded['observation_id']=='sample'
    assert bounded['motion_authorized'] is False
    assert remote.execute_tool('geometry_context',{})['grasp_suggestions']=={'candidates':[1]}
    assert calls==[('geometry_context',dict(session_id='session',timeout_s=GEOMETRY_HTTP_TIMEOUT_S))]
    observation.objects=[]
    assert remote.read_program_target_state()['object_pose'] is None
    remote.pending='outstanding'
    with pytest.raises(HarnessError,match='unresolved_submission'):
        remote.read_program_target_state()
    proxy=OwnerProxy(tmp_path/'unused.sock')
    assert proxy.client.timeout==2 and proxy.preview_client.timeout==32
    assert GEOMETRY_NUMERICAL_TIMEOUT_S < proxy.geometry_client.timeout == GEOMETRY_OWNER_TIMEOUT_S < GEOMETRY_HTTP_TIMEOUT_S


def test_slow_full_geometry_crosses_http_and_both_unix_hops(tmp_path):
    import time
    from rpent.utils.rpc.http_rpc import HttpRpcClient
    from f1_harness.gateway import Gateway, GatewayServer
    from f1_harness.ipc import UnixClient, UnixServer
    from test_unpublished_plan import make_bridge
    engine,bridge,clock,state,_,obs=make_bridge(tmp_path)
    engine.pending=None
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'])
    geometry=dict(observation_id=obs.observation_id,grasp_suggestions={'candidates':[1]},obstacles=[])
    def slow_geometry(name,arguments):
        assert name=='geometry_context' and arguments['observation_id']==obs.observation_id
        time.sleep(2.15)
        return geometry
    plan=UnixServer(tmp_path/'plan.sock',slow_geometry).start()
    client=UnixClient(plan.path,timeout=GEOMETRY_NUMERICAL_TIMEOUT_S)
    bridge.geometry_provider=lambda item,full,held:client.call('geometry_context',observation_id=item.observation_id)
    bridge.serve(tmp_path/'owner.sock')
    gateway=GatewayServer(Gateway(OwnerProxy(tmp_path/'owner.sock'),None,None)).start()
    backend=object.__new__(RemoteTaskBackend)
    backend.rpc=HttpRpcClient(gateway.url,enable_sessions=False)
    backend._await_observation=lambda **kwargs:(obs,{})
    backend.pending=None
    backend.session=engine.session
    try:
        full=backend.execute_tool('geometry_context',{})
        assert full==dict(geometry,search_entities={})
        assert not engine.pending and not engine.running
    finally:
        gateway.close()
        bridge.close()
        plan.close()
