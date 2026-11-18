"""Bootstrap through real owner status/RPent/image transport, without a robot or API.

The owner is an in-memory hardware-contract fixture with no actuator transport.
Only the planner model and camera pixels are synthetic; status dictionaries are
produced by TaskEngine itself so tests cannot invent a top-level generation.
"""
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest
from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.contracts import Frame, Mode, Observation, Task
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.gateway import Gateway, GatewayServer, blob_server
from f1_harness.live_scene import LiveSceneObserver
from f1_harness.task_runner import run_task


PROMPT_REQUIRED = 'mask_view=right:mask_uncertain_requires_new_prompt:object=square-1'


@contextmanager
def owner_rpc_fixture(tmp_path):
    store = EvidenceStore(tmp_path / 'owner')
    store.event('synthetic_test_contract', robot_transport=False, model_api=False)
    engine = TaskEngine(store, mode=Mode.HARDWARE, lease_ms=30_000)
    # Admission is simulated only inside this local object. There is no control
    # transport, ROS node, device, physical controller or network robot address.
    engine.hardware_ready = True
    engine.web_control_enabled = True
    engine.recording_identity = dict(owner_run_id='synthetic-bootstrap-contract')
    frames = [Frame(camera=camera, stream='rgb', frame_id=camera, sequence=1,
        source_ns=1, received_ns=engine.clock(), clock_domain='synthetic',
        width=64, height=48, encoding='rgb8',
        blob_sha256=store.put_blob(bytes([80, 120, 160]) * (64 * 48)))
        for camera in ('head', 'right')]

    raw = Observation(observation_id='synthetic-camera-only',
        acquisition_instance='synthetic-capture', received_ns=engine.clock(),
        host_boot_id='synthetic-boot', clock_domain='synthetic',
        synthetic=True, frames=tuple(frames))
    observer = LiveSceneObserver.__new__(LiveSceneObserver)
    observer.raw = SimpleNamespace(store=store, observe=lambda: raw)
    observer.store = store
    observer.owner = SimpleNamespace(call=lambda method: engine.status())
    observer.lock = threading.Lock()
    observer.latest, observer.error = None, PROMPT_REQUIRED
    observer.image_issuances = {}
    observer.config = dict(max_age_ms=500, geometry=dict(camera='right',
        roi=[0, 0, 64, 48], object_id='square-1',
        k_rgb=[[60., 0., 32.], [0., 60., 24.], [0., 0., 1.]], d_rgb=[0.] * 5))
    from f1_harness.mask_service import ConfiguredMaskProvider
    # Real prompt catalog, with no GPU inference in this synthetic RPC fixture.
    observer.pipeline = SimpleNamespace(segment=ConfiguredMaskProvider({'views':{'right':dict(
        backend='sam2_remote',image_shape=[48,64,3],sam_objects=['square-1','square-hole-1'],
        prompts={'square-1':[16,12,48,36],'square-hole-1':[2,2,12,12]},color_profiles={})}},
        segmenter={'right':object()}))

    calls = []

    class ReadOnlyTestGateway(Gateway):
        def dispatch(self, method, args=(), kwargs=None, **transport):
            calls.append(method)
            assert method in {
                'capabilities', 'open_session', 'prepare_task', 'heartbeat', 'status',
                'observe_with_status', 'observe', 'camera_observation', 'execution_events',
                'finish_task', 'close_session', 'locate_target',
            }, 'This fixture forbids every motion request'
            value = super().dispatch(method, args=args, kwargs=kwargs, **transport)
            if method == 'prepare_task':
                # Exercise the real preparation -> authorization contract only
                # in this in-memory engine. This is never sent to a robot.
                engine.authorize_web(request_id='synthetic-request',
                    run_id='synthetic-bootstrap-contract', feedback_healthy=True)
            return value

    gateway = GatewayServer(ReadOnlyTestGateway(engine, observer, store)).start()
    images = blob_server(store)
    try:
        yield engine, observer, gateway.url, f'http://127.0.0.1:{images.server_address[1]}', calls
    finally:
        gateway.close()
        images.shutdown()
        images.server_close()


@pytest.mark.parametrize('change_generation', [False, True])
def test_real_owner_status_reaches_model_images_and_checks_next_round(
        tmp_path, monkeypatch, change_generation):
    """ce5ff: actual status has generation in perception_context, never at root."""
    with owner_rpc_fixture(tmp_path) as (engine, observer, gateway, images, wire_calls):
        def make_arm(backend, workspace, **kwargs):
            arm = ArmToolkit(F1Robot(backend), arm='cells', workspace=workspace,
                             images='on-demand')
            arm.start = lambda: arm
            arm.close = lambda: None
            return arm

        monkeypatch.setattr('f1_harness.task_runner.SandboxedArm', make_arm)
        model_rounds = []

        class NoNetworkPlanner:
            def __init__(self, *args):
                pass

            def prepare_connection(self):
                pass

            def close(self):
                pass

            def run(self, arm, *, task_text, snapshot_text, continue_run, complete_run=None):
                status = engine.status()
                assert 'task_generation' not in status
                assert status['perception_context']['task_generation'] == 1
                assert status['perception_context']['authorized'] is True
                assert continue_run()
                assert self.initial_visual_needed
                assert 'Initial geometry is unavailable' in task_text
                assert 'search_entities' in task_text and 'square-hole-1' in task_text
                model_rounds.append('image-recovery')
                # Real PyRUA -> F1 adapter -> EvidenceRemoteBackend -> RPent
                # HTTP -> raw image -> separate HTTP blob path, with no geometry.
                result = arm.execute_tool('python', {'code': 'robo.show("right")'})
                assert result.images and not result.result.get('exception')
                result = arm.execute_tool('python', {'code':
                    'print(robo.locate_object("right", [0.25, 0.25, 0.75, 0.75]))'})
                assert not result.result.get('exception')
                hint = observer.pending_target_hints["right"]
                assert hint['hint']['box'] == [16, 12, 48, 36]
                assert hint['hint']['motion_evidence'] is False
                assert hint['context']['task_generation'] == 1
                assert observer.latest is None
                result = arm.execute_tool('python', {'code':
                    "robo.locate_entities('right', [{'object_id':'square-hole-1', "
                    "'box_normalized':[.1,.1,.3,.3], 'points_normalized':[[.2,.2]], 'point_labels':[1]}])"})
                assert not result.result.get('exception')
                batch=observer.pending_target_hints['right']['hint']['entity_hints']
                assert {item['object_id'] for item in batch}=={'square-1','square-hole-1'}
                batch=[item for item in batch if item['object_id']=='square-hole-1']
                assert batch[0]['object_id']=='square-hole-1'
                assert batch[0]['point_prompt']['labels']==[1]
                if change_generation:
                    # Even without an epoch change, preparation identity changes
                    # must be detected at the next model/tool-round boundary.
                    engine.task_generation += 1
                assert continue_run() is (not change_generation)
                return dict(outcome='pending_review', reason='synthetic read-only model')

        monkeypatch.setattr('f1_harness.planner.ResponsesPlanner', NoNetworkPlanner)
        task = Task(task_id='contract-test', object_id='square-1',
                    hole_id='square-hole-1', calibration_id='synthetic-calibration')
        task_path = tmp_path / 'task.json'
        task_path.write_text(task.model_dump_json())
        store = EvidenceStore(tmp_path / 'planner')
        args = SimpleNamespace(task=task_path, gateway=gateway, image_gateway=images,
            authorization_source='web', start_timeout=2, planner='astra',
            experience_snapshot=None)

        result = run_task(args, store, None)

        assert model_rounds == ['image-recovery']
        assert result['measured']['outcome'] == 'pending_review'
        assert not engine.pending and not engine.running and not engine.request_publications
        assert wire_calls.count('observe_with_status') == 1
        assert 'camera_observation' in wire_calls
        assert 'locate_target' in wire_calls
        events = [json.loads(line) for line in (store.root / 'trace.jsonl').read_text().splitlines()]
        assert any(e['kind'] == 'observation_prompt_required' for e in events)
        assert any(e['kind'] == 'planner_initial_observation_unavailable' for e in events)
        assert any(e['kind'] == 'planner_image_requested' for e in events)
        assert not any(e['kind'] == 'observation_readiness_timeout' for e in events)
