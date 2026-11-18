import httpx
from pyrualean.mcp_server import ArmMcpServer
from f1_harness.host import fake_host
from f1_harness.sandbox import SandboxedArm


def test_actual_mcp_tools_are_not_mislabelled_readonly(tmp_path):
    e,p,b,_=fake_host(tmp_path)
    arm=SandboxedArm(b,tmp_path/'task').start()
    server=ArmMcpServer(arm,annotations=dict(readOnlyHint=False,destructiveHint=True,idempotentHint=False,openWorldHint=False))
    try:
        url=server.start()
        response=httpx.post(url,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},
                            headers={'Accept':'application/json'},trust_env=False)
        tools=response.json()['result']['tools']
        assert {t['name'] for t in tools}=={'python','finish'}
        assert all(t['annotations']['readOnlyHint'] is False for t in tools)
    finally:server.stop();arm.close()
