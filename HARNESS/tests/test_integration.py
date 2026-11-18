import pytest
from pyrualean.arms import ArmToolkit
from f1_harness.adapters import F1Robot
from f1_harness.host import fake_host
from f1_harness.programs import SINGLE_BLOCK
from f1_harness.sandbox import SandboxedArm
from f1_harness.gateway import Gateway, GatewayServer
from rpent.utils.rpc.http_rpc import HttpRpcClient


@pytest.mark.parametrize('failures,success', [({},True),({'grasp':1,'insert':2},True),
    ({'verify_grasp':1},True),({'insert':3},False)])
def test_actual_upstream_offline(tmp_path, failures, success):
    e,p,b,_=fake_host(tmp_path,failures=failures)
    arm=ArmToolkit(F1Robot(b),arm='cells',workspace=tmp_path/'task',images=False)
    value=arm.execute_tool('python',{'code':SINGLE_BLOCK})
    assert b.solved() is success
    assert (e.finish_task(e.session)['outcome']=='success') is success
    assert not value.result.get('exception')  # Task failure remains measured, not a Python crash.


def test_rpent_http_cannot_start_or_publish(tmp_path):
    e,p,_,store=fake_host(tmp_path)
    server=GatewayServer(Gateway(e,p,store)).start()
    try:
        client=HttpRpcClient(server.url,enable_sessions=False)
        assert client.call('capabilities')['raw_motion'] is False
        assert client.call('observe')['synthetic'] is True
        with pytest.raises(Exception,match='RPC_method_not_available'):
            client.call('local_start',kwargs={'physical_event':True,'feedback_healthy':True})
    finally:server.close()


def test_combined_gateway_read_carries_attention_without_motion(tmp_path):
    e,p,_,store=fake_host(tmp_path)
    condition=dict(state='needs_attention',code='camera_identity_changed')
    p.readiness=lambda:condition
    result=Gateway(e,p,store).dispatch('observe_with_status')
    assert result['status']['observation_readiness']==condition
    assert result['observation']['synthetic'] is True


def test_bwrap_isolation_and_external_infinite_loop_deadline(tmp_path):
    e,p,b,_=fake_host(tmp_path)
    arm=SandboxedArm(b,tmp_path/'task',timeout_s=.4).start()
    try:
        audit=arm.audit()
        assert not audit['credential_visible'] and not audit['host_home_visible']
        assert 'OPENAI_API_KEY' not in audit['environment_keys']
        assert audit['pid']==2
        with pytest.raises(RuntimeError,match='authority_revoked'):
            arm.execute_tool('python',{'code':'while True: pass'})
        assert arm.process.poll() is not None and not e.authorized
    finally:arm.close()
