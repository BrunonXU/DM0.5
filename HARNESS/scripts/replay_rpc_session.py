"""Formal HTTP/Unix task wiring for the memory-only native owner replay.

The real TaskEngine uses its hardware CONTRACT in this local fixture, like the
bootstrap contract tests. The only actuator transport is MemoryTransport. No
robot address, ROS node or physical publisher is constructed here.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from f1_harness.gateway import Gateway,GatewayServer,blob_server
from f1_harness.remote import OwnerProxy
from f1_harness.contracts import Mode, Observation
from f1_harness.ipc import UnixClient


class PerceptionProcess:
    """Trusted local image worker in its own dependency environment, no API key."""
    def __init__(self,ns,channel):
        self.process=None;self.log=None
        root=ns['root'];args=ns['args'];run=ns['run']
        config=json.loads(args.rpc_perception_config.read_text())
        self.client=UnixClient(Path(channel)/'perception.sock',timeout=15,maximum=512*1024,scene_evidence=True)
        command=[str(root/'.venv/bin/python'),str(root/'scripts/replay_perception_worker.py'),
            '--socket',self.client.path,'--capture',str(args.rpc_capture),
            '--native-feedback',config['native_feedback'],'--mask-config',config['mask_config'],
            '--sam-url',config['sam_url'],'--output',str(run/'perception'),
            '--store',str(ns['store'].root)]
        for key in ('native_feedback','mask_config'):
            path=Path(config[key]).resolve()
            ns['source_files'][str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        self.log=(run/'perception-process.log').open('x')
        env=dict(PATH=os.environ.get('PATH','/usr/bin:/bin'),PYTHONPATH=str(root/'src'),
            PYTHONNOUSERSITE='1',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
        try:
            self.process=subprocess.Popen(command,env=env,stdout=self.log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+20
            while not Path(self.client.path).exists():
                if self.process.poll() is not None:raise RuntimeError('offline_perception_worker_exited')
                if time.monotonic()>deadline:raise TimeoutError('offline_perception_worker_boot_timeout')
                time.sleep(.02)
            if self.client.call('ready')!={'ready':True,'actuator':False}:
                raise ValueError('unexpected_perception_worker_contract')
        except BaseException:self.close();raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        if self.log is not None:self.log.close()

    def call(self,method,**kwargs):
        # The production observer is in-process at this boundary. Preserve its
        # ValueError contract across the fixture's dependency-isolation IPC.
        try:return self.client.call(method,**kwargs)
        except RuntimeError as exc:
            if str(exc).startswith('ValueError: '):
                raise ValueError(str(exc).removeprefix('ValueError: ')) from exc
            raise


def run_rpc_session(ns):
    args=ns['args'];engine=ns['engine'];bridge=ns['bridge'];store=ns['store'];run=ns['run']
    assert args.rpc_planner and args.wall_clock and args.follower=='approved-output'
    assert engine.mode is Mode.HARDWARE and ns['output_owner'] is not None
    assert type(ns['output_owner'].transport).__name__=='MemoryTransport'
    root=ns['root'];original=ns['original'];clock=ns['clock'];outcomes=ns['outcomes']
    # Only original image artifacts are shown, and only before any motion.
    capture=args.rpc_capture
    for frame in original.frames:
        for sha in (frame.blob_sha256,frame.camera_info_sha256):
            if sha:
                data=(capture.parent/'blobs'/sha).read_bytes()
                if hashlib.sha256(data).hexdigest()!=sha:raise ValueError('recorded_blob_identity')
                assert store.put_blob(data)==sha
    perception=None
    class Observer:
        def aperture_observation(self):
            view = ns.get('aperture_viewpoint')
            result = dict(camera='left', available=False, motion_evidence=False, candidates={})
            if view is not None and view.move is not None:
                result['observation_move'] = dict(view.move)
            return result

        def observe(self):
            if perception is not None and not engine.request_publications:
                result=perception.call('observe',status=engine.status())
                if result['source_clock_unchanged'] is not True:raise ValueError('recording_source_clock_changed')
                scene=Observation.model_validate(result['scene'])
                consumed=ns['install_perception_scene'](scene)
                if not consumed.wait(2):raise TimeoutError('offline_owner_did_not_consume_perception')
            deadline=time.monotonic()+2
            while engine.live_observation is None and time.monotonic()<deadline:time.sleep(.01)
            if engine.live_observation is None:raise ValueError('offline_observation_not_ready')
            # Gateway commits this snapshot on an actual observe RPC. The
            # control loop only refreshes the independent live scene lane.
            return engine.live_observation
        def camera_observation(self):
            if engine.request_publications:
                raise ValueError('offline_motion_camera_frames_unavailable')
            if perception is not None:return perception.call('camera_observation',status=engine.status())
            return original.model_dump(mode='json')
        def locate_target(self,**kwargs):
            if perception is not None:
                if engine.request_publications:raise ValueError('offline_motion_camera_frames_unavailable')
                return perception.call('locate_target',status=engine.status(),**kwargs)
            raise ValueError('offline_saved_scene_has_no_new_camera_localization')
        def readiness(self):
            if perception is not None and not engine.request_publications:
                return perception.call('readiness',status=engine.status())
            return dict(state='ready',code='offline_simulated_motion_scene',search_entities={})
    submitted={};seen=set();calls=[]
    class ContractGateway(Gateway):
        def dispatch(self,method,args=(),kwargs=None,**transport):
            kwargs=kwargs or {};calls.append(dict(method=method,virtual_ns=clock[0]))
            value=super().dispatch(method,args=args,kwargs=kwargs,**transport)
            if method=='prepare_task':
                ns['session']=engine.session
                engine.authorize_web(request_id='offline-fixture-only',
                    run_id=engine.recording_identity['owner_run_id'],feedback_healthy=True)
            if method=='submit_skill':
                intent=kwargs['intent']
                submitted[intent['request_id']]=(dict(intent),clock[0],time.monotonic())
            if method=='skill_result' and value['lifecycle'] not in ('accepted','running','paused'):
                request=kwargs['request_id']
                if request in submitted and request not in seen:
                    seen.add(request);intent,start,wall=submitted[request]
                    outcomes.append(dict(skill=intent['skill'],request=intent,result=value,
                        virtual_s=(clock[0]-start)/1e9,wall_s=time.monotonic()-wall,
                        source='actual_RPent_skill_result'))
                    print('rpc_skill',intent['skill'],value['outcome'],value['reason'],flush=True)
            return value
    process=None;gateway=None;images=None;log=None
    with tempfile.TemporaryDirectory(prefix='f1-offline-rpc-') as channel:
        if args.rpc_perception_config:perception=PerceptionProcess(ns,channel)
        bridge.geometry_provider=lambda obs,full,held:ns['service'].dispatch('geometry_context',dict(
            observation=obs.model_dump(),full22=full,held16=held))
        bridge.serve(Path(channel)/'owner.sock')
        gateway=GatewayServer(ContractGateway(OwnerProxy(Path(channel)/'owner.sock'),Observer(),store)).start()
        images=blob_server(store)
        command=[str(root/'.venv/bin/python'),str(root/'scripts/replay_rpc_client.py'),
            '--task',str(args.site/'task.local.json'),'--gateway',gateway.url,
            '--image-gateway',f'http://127.0.0.1:{images.server_address[1]}',
            '--output',str(run/'planner')]
        if args.rpc_planner=='recorded':command+=['--recorded-request',str(args.approach_request)]
        for receipt in args.rpc_hint_receipt:command+=['--hint-receipt',str(receipt)]
        try:
            log=(run/'planner-process.log').open('x')
            process=subprocess.Popen(command,env=dict(os.environ,PYTHONPATH=str(root/'src')),
                                     stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+args.max_wall_seconds+20
            while process.poll() is None:
                if time.monotonic()>deadline:raise TimeoutError('offline_rpc_process_budget_exceeded')
                if engine.session and engine.task:ns['tick']()
                else:time.sleep(.01)
            if process.returncode:raise RuntimeError('offline_rpc_client_failed:'+str(process.returncode))
            result=json.loads((run/'planner/result.json').read_text())['result']
            # Continue memory-only owner ticks until the actual finish hold is confirmed.
            for _ in range(100):
                if not engine.hold_requested:break
                ns['tick']()
            return result['measured']
        finally:
            if process is not None and process.poll() is None:
                engine.revoke('offline_rpc_cleanup');process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            if gateway is not None:gateway.close()
            if images is not None:images.shutdown();images.server_close()
            if log is not None:log.close()
            if perception is not None:perception.close()
            (run/'rpc-calls.json').write_text(json.dumps(calls,indent=2))
