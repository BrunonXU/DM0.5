import threading
from f1_harness.contracts import Mode,Task,Observation
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.fake import FakePlant
from f1_harness.ipc import UnixServer
from f1_harness.gateway import Gateway,GatewayServer,OWNER_METHODS
from f1_harness.remote import OwnerProxy,RemoteTaskBackend
from f1_harness.adapters import F1Toolkit,ToolkitBackend
from f1_harness.sandbox import SandboxedArm
from f1_harness.programs import SINGLE_BLOCK


def test_sandbox_rpent_http_unix_owner_mailbox_closed_loop(tmp_path):
    store=EvidenceStore(tmp_path)
    engine=TaskEngine(store,mode=Mode.FAKE)
    plant=FakePlant(engine,{'insert':1})
    def dispatch(method,kwargs):
        if method=='ingest_observation':
            engine.update_observation(Observation.model_validate(kwargs['observation']));return {}
        if method in OWNER_METHODS:return getattr(engine,method)(**kwargs)
        raise ValueError('forbidden')
    owner=UnixServer(tmp_path/'owner.sock',dispatch).start()
    gateway=GatewayServer(Gateway(OwnerProxy(tmp_path/'owner.sock'),plant,store)).start()
    backend=RemoteTaskBackend(gateway.url,Task(task_id='transport',object_id='square',hole_id='hole',calibration_id='fake'))
    assert not engine.authorized
    plant.start()  # Explicit fake-owner event; not a gateway method.
    stop=threading.Event()
    def loop():
        while not stop.wait(.002):plant.advance()
    thread=threading.Thread(target=loop,daemon=True);thread.start()
    arm=SandboxedArm(ToolkitBackend(F1Toolkit(backend,tmp_path/'memory')),tmp_path/'worker').start()
    try:
        result=arm.execute_tool('python',{'code':SINGLE_BLOCK})
        assert 'exception' not in result.result
        assert backend.solved() and engine.corrections==1
        assert engine.finish_task(engine.session)['outcome']=='success'
    finally:
        arm.close();stop.set();thread.join(1);gateway.close();owner.close()
