"""Full baseline/owner/native-geometry simulation, never hardware acceptance.

Geometry starts from a saved scene. Subsequent encoders, camera observations and
attachment dynamics are test fixtures, with ideal actuator following. Optional
approved-output mode runs the real F1Owner/Ruckig before this ideal actuator. As in
hardware-contract unit tests, mock geometry has synthetic=False so UNMODIFIED
hardware validators run; this field must not be interpreted as real acquisition.
TaskEngine stays FAKE and trace rows are tagged simulation. Native failure
results may retain synthetic=False; report their annotations separately.
This is not camera, plant, ROS, RPC or full ControlLoop validation.
"""
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json
import math
import time
PROCESS_ENTRY_NS=time.monotonic_ns()
import threading
import uuid
import argparse
import hashlib
import sys
import numpy as np
from replay_evidence import replay_provenance
from f1_harness.contracts import Observation, Task, SkillIntent, SkillResult, Mode, MotionParameters, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.engine import TaskEngine
from f1_harness.owner_bridge import OwnerBridge, JointProposal
from f1_harness.skill_services import PlanService, VerifyService
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.geometry import transform, insertion_depth_reached
from f1_harness.programs import single_block_program

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--site',type=Path,required=True,help='Local task/plan/gateway/numerics config directory')
parser.add_argument('--scene',type=Path,required=True,help='Saved chain-inputs JSON array; never fetches from robot')
parser.add_argument('--scene-index',type=int,default=0)
parser.add_argument('--approach-request',type=Path,
                    help='Recorded preview_skill request whose approach parameters feed the registered program')
parser.add_argument('--rpc-planner',choices=['recorded','astra'],
                    help='Run the formal task runner against a local memory-only hardware-contract fixture')
parser.add_argument('--rpc-capture',type=Path,help='Original capture.json for pre-motion image blobs')
parser.add_argument('--rpc-perception-config',type=Path,
                    help='Local actual-SAM worker config; original recording before motion, explicit simulation afterwards')
parser.add_argument('--rpc-hint-receipt',type=Path,action='append',default=[],
                    help='Saved actual localization receipt for recorded planner only')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--defer-fixture-cyclic-gc',action='store_true',
    help='Defer cyclic GC of retained offline audit objects until after the task; not production latency evidence')
parser.add_argument('--expect-outcome',choices=['success','pending_review','failure','interrupted'],default='success')
parser.add_argument('--program',choices=['legacy','registered'],default='registered',
                    help='Use the registered PyRUA task helper; legacy is an explicit historical comparison')
parser.add_argument('--grasp-observation-evidence',choices=['ideal','recover_after_motion','always_unknown','miss_once_then_observe'],default='ideal',
                    help='Simulated attachment evidence loss; not a camera/segmentation replay')
parser.add_argument('--wall-clock',action='store_true',
                    help='Offline real-time ticks and asynchronous planner, with separate native model instances')
parser.add_argument('--max-wall-seconds',type=float,default=60.,help='Offline run deadline, not a robot motion gate')
parser.add_argument('--plan-delay-s',type=float,default=0.,help='Offline worker delay injection; wall-clock only')
parser.add_argument('--cancel-during-plan',action='store_true',help='Offline cancellation injection; wall-clock only')
parser.add_argument('--follower',choices=['ideal','approved-output'],default='ideal',
                    help='Use ideal references or the real F1Owner/Ruckig with an in-memory transport')
parser.add_argument('--object-offset-mm',type=float,nargs=3,default=[0.,0.,0.],metavar=('X','Y','Z'),
                    help='Synthetic offset in the saved native frame, not measured scene variation')
parser.add_argument('--hole-offset-mm',type=float,nargs=3,default=[0.,0.,0.],metavar=('X','Y','Z'))
parser.add_argument('--missing-scene-at',default=None)
parser.add_argument('--occlude-aperture-after-transfer',action='store_true',
                    help='Hide the simulated aperture until the held arm moves sideways above the bucket')
parser.add_argument('--missing-scene-calls',type=int,default=0)
parser.add_argument('--preview-segments',action='store_true',
                    help='Offline forward-only segment forecasts from current committed smoother state')
parser.add_argument('--preview-hold-samples',type=int,default=0,
                    help='Conditional repeated endpoint references after each forecast boundary (0..250)')
parser.add_argument('--recommend-segment-speeds',type=int,nargs='+',default=None,
                    help='Offline same-state conditional segment comparison; never changes the running plan')
parser.add_argument('--segment-preferences',type=Path,default=None,
                    help='Optional frozen offline ordering hints; never previous collision verdicts')
parser.add_argument('--candidate-audit-workers',type=int,choices=(0,1,2),default=0,
                    help='Local prewarmed numerical audit processes, diagnostic recommendations only')
parser.add_argument('--candidate-assets',type=Path,default=None,
                    help='Prepared read-only numerical dependency snapshot for audit workers')
parser.add_argument('--consume-simulated-segments',action='store_true',
                    help='Experimental FAKE-only reference timing consumption, never production authorization')
parser.add_argument('--prefetch-grasp',action='store_true',help='FAKE wall-clock only: one speculative grasp candidate')
parser.add_argument('--prefetch-transfer',action='store_true',help='FAKE only: reuse speculative worker for grasp-to-transfer')
parser.add_argument('--transfer-prefetch-delay-s',type=float,default=0.,help='Offline transfer speculation delay injection (0..5 seconds)')
parser.add_argument('--prefetch-delay-s',type=float,default=0.,help='Offline speculative worker delay (0..5 seconds)')
parser.add_argument('--cancel-during-prefetch',action='store_true',help='Offline revoke while a speculative job is active')
parser.add_argument('--simulated-gripper-can',action='store_true',help='Exercise published-close CAN verdicts from the offline actuator plant')
parser.add_argument('--clock-origin-ns',type=int,default=10_000_000_000,
                    help='Initial monotonic clock for offline time-origin comparisons')
parser.add_argument('--tick-pattern-ns',type=int,nargs='+',default=[20_000_000],
                    help='Repeating actual sample intervals for offline scheduling experiments')
parser.add_argument('--real-numerics-ipc',action='store_true',
                    help='Route PlanService numerical calls over a real local UnixServer/UnixClient JSON IPC socket')
args=parser.parse_args()
# Only the trusted local sequence driver injects this callable via runpy. It
# creates no socket service or hardware entry and cannot change FAKE mode.
prefetch_factory=globals().get('_offline_prefetch_factory')
if prefetch_factory is not None and not (args.prefetch_grasp and args.wall_clock and args.follower=='approved-output'):
    parser.error('prewarmed worker requires the same FAKE approved-output wall-clock contract')
if (args.prefetch_grasp and (not args.wall_clock or args.follower!='approved-output')
        or (args.prefetch_delay_s or args.cancel_during_prefetch) and not args.prefetch_grasp):
    parser.error('grasp prefetch requires approved-output FAKE wall-clock replay')
if not math.isfinite(args.prefetch_delay_s) or not 0<=args.prefetch_delay_s<=5:
    parser.error('prefetch delay must be between zero and five seconds')
if args.prefetch_transfer and (not args.prefetch_grasp or prefetch_factory is not None):
    parser.error('transfer speculation requires direct FAKE grasp worker; warm leases are not yet supported')
if (not math.isfinite(args.transfer_prefetch_delay_s) or not 0<=args.transfer_prefetch_delay_s<=5
        or args.transfer_prefetch_delay_s and not args.prefetch_transfer):
    parser.error('transfer delay requires transfer speculation and must be between zero and five seconds')
if not math.isfinite(args.max_wall_seconds) or not 0<args.max_wall_seconds<=300:
    parser.error('offline wall deadline must be positive and at most 300 seconds')
if not math.isfinite(args.plan_delay_s) or not 0<=args.plan_delay_s<=5:
    parser.error('offline plan delay must be between 0 and 5 seconds')
if (args.plan_delay_s or args.cancel_during_plan) and not args.wall_clock:
    parser.error('asynchronous injections require wall-clock mode')
if args.wall_clock and (args.preview_segments or args.tick_pattern_ns!=[20_000_000]):
    parser.error('wall-clock mode cannot freeze time for synchronous candidate previews or prescribed jitter')
if args.rpc_planner and (not args.wall_clock or args.follower!='approved-output' or args.rpc_capture is None
        or args.prefetch_grasp or args.preview_segments or args.program!='registered'
        or args.rpc_planner=='recorded' and args.approach_request is None):
    parser.error('RPC fixture requires wall-clock, approved-output, capture, registered program; no speculative mode')
if args.rpc_perception_config and (not args.rpc_planner or any(args.object_offset_mm+args.hole_offset_mm)):
    parser.error('actual perception requires formal RPC without synthetic scene offsets')
if args.rpc_hint_receipt and not (args.rpc_perception_config and args.rpc_planner=='recorded'):
    parser.error('saved localization requires recorded planner and actual perception')
if args.clock_origin_ns<0:parser.error('clock origin must be nonnegative')
if (len(args.tick_pattern_ns)>32 or any(v<=0 or v>1_000_000_000 for v in args.tick_pattern_ns)):
    parser.error('tick pattern requires one to 32 positive intervals no greater than one second')
if args.preview_segments and any(v!=20_000_000 for v in args.tick_pattern_ns):
    parser.error('existing segment preview declares 50Hz; jitter replay must not claim that forecast')
if args.preview_segments and args.follower!='approved-output':parser.error('segment previews require approved-output')
if not 0<=args.preview_hold_samples<=250:parser.error('preview-hold-samples must be 0..250')
if args.preview_hold_samples and not args.preview_segments:parser.error('hold forecasts require preview-segments')
if args.recommend_segment_speeds is not None and not (args.preview_segments and args.preview_hold_samples):
    parser.error('segment recommendations require previews with a conditional hold')
if args.segment_preferences is not None and args.recommend_segment_speeds is None:
    parser.error('segment preferences require segment recommendations')
if args.candidate_audit_workers and args.recommend_segment_speeds is None:
    parser.error('candidate audit workers require segment recommendations')
if args.candidate_assets is not None and not args.candidate_audit_workers:
    parser.error('candidate assets require audit workers')
if args.consume_simulated_segments and args.recommend_segment_speeds is None:
    parser.error('simulated consumption requires conditional segment recommendations')
if args.missing_scene_calls<0:parser.error('missing-scene-calls must be nonnegative')
if not np.isfinite([args.object_offset_mm,args.hole_offset_mm]).all():parser.error('offsets must be finite')
injections=[]
root=Path(__file__).resolve().parents[1]
area=args.output.resolve();area.mkdir(parents=True,exist_ok=True)
site=args.site.resolve();source_path=args.scene.resolve()
data=json.loads(source_path.read_text())[args.scene_index]
approach_parameters={}
if args.approach_request is not None:
    selected=json.loads(args.approach_request.read_text())
    if args.program!='registered' or selected.get('name')!='approach_grasp':
        parser.error('approach request requires registered program and approach_grasp')
    if 'candidates' in selected and args.rpc_planner!='recorded':
        parser.error('candidate-list program requires the recorded formal RPC planner')
    approach_parameters=MotionParameters.model_validate(selected.get('parameters',{})).for_skill('approach_grasp').supplied()
original=Observation.model_validate(data['observation'])
task=Task.model_validate_json((site/'task.local.json').read_text())
if task.goal!='placed' or 'withdraw_reset' not in task.allowed_skills:
    parser.error('full-cycle replay requires an explicit placed task including withdraw_reset')
fixture=json.loads((site/'plan.local.json').read_text())['fixture']
frame_owner_original=fixture['frame_chain'].get('development_owner_config')
if frame_owner_original:
    # Relocate only the resolver for offline validation. Do not rewrite the
    # source config, its hashes or the original coarse calibration receipt.
    fixture['frame_chain']['development_owner_config']=str(site/'owner.local.json')
if args.missing_scene_at and args.missing_scene_at not in task.allowed_skills:
    parser.error('missing-scene-at must name an allowed skill')
geometry=json.loads((site/'gateway.local.json').read_text())['scene']['geometry']
spec=json.loads((site/'numerics.local.json').read_text())['spec']
spec['urdf']=str(root.parent/'F1-4242/web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf')
spec['gate']['config']['_infra_root']=str((root.parent/'F1-4242').resolve())
spec['gate']['runtime']=str(area/'native-model');spec['gate']['full22']=data['full22']
numeric=NumericalDispatcher(spec)
planning_numeric=numeric
source_files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [
    Path(__file__),Path(__file__).with_name('replay_evidence.py'),source_path,*sorted(site.glob('*.json')),
    Path(sys.modules[type(numeric.numerics).__module__].__file__),
    *sorted((root/'src/f1_harness').glob('*.py'))]}
if args.approach_request is not None:
    p=args.approach_request.resolve();source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
if args.rpc_perception_config:
    for p in [args.rpc_perception_config,*args.rpc_hint_receipt,
              Path(__file__).with_name('replay_perception_worker.py')]:
        source_files[str(p.resolve())]=hashlib.sha256(p.read_bytes()).hexdigest()
clock=[args.clock_origin_ns];tick_count=0;started=time.perf_counter();computations=[];publications=[];plans=[];owner_outputs=[]
segment_previews=[];previewed_segments=set();segment_recommendations=[];segment_preferences=[]
preference_snapshot=dict(loaded=False)
if args.segment_preferences is not None:
    try:
        raw=args.segment_preferences.read_bytes()
        value=json.loads(raw)
        if value.get('schema')!='f1-offline-segment-preferences-v1' or not isinstance(value.get('items'),list):
            raise ValueError('invalid_preference_collection')
        segment_preferences=value['items']
        preference_snapshot=dict(loaded=True,path=str(args.segment_preferences.resolve()),sha256=hashlib.sha256(raw).hexdigest())
    except (OSError,ValueError,TypeError,AttributeError) as exc:
        preference_snapshot=dict(loaded=False,reason=type(exc).__name__)
if args.preview_segments:
    from segment_preview import preview_segment
    for name in ('segment_preview.py','output_branch.py','output_sweeps.py','../src/f1_harness/command_forecast.py'):
        p=(Path(__file__).parent/name).resolve();source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
if args.recommend_segment_speeds is not None:
    from segment_candidates import recommend_segment,audit_candidate,state_binding
    for name in ('segment_candidates.py','sweep_blocks.py','segment_preferences.py','audit_workers.py','audit_worker_launch.py','audit_assets.py','audit_asset_watch.py'):
        p=Path(__file__).with_name(name);source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
audit_pool=None
choices=None
if args.consume_simulated_segments:
    from consume_simulated_segments import SimulatedSegmentChoices
    from unittest.mock import patch
    choices=SimulatedSegmentChoices('fake')
    p=Path(__file__).with_name('consume_simulated_segments.py')
    source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
if args.candidate_audit_workers:
    import atexit
    from audit_workers import AuditPool
    audit_pool=AuditPool(spec,area/'candidate-audit-workers',workers=args.candidate_audit_workers,
                        assets=args.candidate_assets)
    atexit.register(audit_pool.close)
    audit_pool.start()
class Store(EvidenceStore):
    def event(self,kind,**data):
        row=dict(event_id=uuid.uuid4().hex,kind=kind,monotonic_ns=clock[0],**data)
        row['simulation']=True
        with (self.root/'trace.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
        return row['event_id']
run=area/('sim-'+uuid.uuid4().hex[:10]);store=Store(run/'collected/owner/harness')
engine=TaskEngine(store,mode=Mode.HARDWARE if args.rpc_planner else Mode.FAKE,clock=lambda:clock[0])
session=None
if args.rpc_planner:
    engine.hardware_ready=True;engine.web_control_enabled=True
    engine.recording_identity=dict(owner_run_id=run.name,host_boot_id=engine.host_boot_id,
        offline_fixture=True,actuator_transport='memory')
    store.event('offline_hardware_contract_fixture',physical_motion=False,actuator_transport='memory')
    for name in ('replay_rpc_session.py','replay_rpc_client.py'):
        p=Path(__file__).with_name(name);source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
else:
    session=engine.open_session()['session_id'];engine.prepare_task(session,task.model_dump())
    engine.local_start(physical_event=True,feedback_healthy=True)  # FAKE engine only.
assert engine.mode is (Mode.HARDWARE if args.rpc_planner else Mode.FAKE)

class Direct:
    def call(self,method,**kwargs):return planning_numeric.dispatch(method,kwargs)
# Construct the real service so configuration validation is not bypassed.
numerics_ipc_server=None
if args.real_numerics_ipc:
    from f1_harness.ipc import UnixServer
    numerics_sock=area/'numerics.sock'
    numerics_ipc_server=UnixServer(numerics_sock,planning_numeric.dispatch).start()
    service=PlanService(task,fixture,numerics_sock,clock=lambda:clock[0])
else:
    # The client is replaced before any calls: only pure local numerics run.
    service=PlanService(task,fixture,area/'unused-numerics.sock',clock=lambda:clock[0])
    service.numerics=Direct()
verifier=VerifyService(task,clock=lambda:clock[0],independent_grasp=args.simulated_gripper_can)
verifier_dropout=threading.Event()
gripper_plant=None
if args.simulated_gripper_can:
    from replay_gripper_can import GripperPlant
    helper=Path(__file__).with_name('replay_gripper_can.py')
    source_files[str(helper)]=hashlib.sha256(helper.read_bytes()).hexdigest()
    gripper_plant=GripperPlant(verifier,store,json.loads((site/'gateway.local.json').read_text())['scene'])
full=np.array(data['full22']);measured=np.array(original.state16);held=measured.copy()
output_owner=None;output_profile=None
if args.follower=='approved-output':
    from types import SimpleNamespace
    from f1dagger.robot_io import F1Owner,make_output
    class MemoryTransport:
        # This class has no socket, ROS publisher, device handle or networking.
        rmw_gids={k:'simulation' for k in ('joint_command','left_gripper_command','right_gripper_command')}
        own_gids={}
        def prepare(self,key):pass
        def assert_exclusive_cached(self,key):pass
        def publish(self,key,value):pass
        def release(self,key):pass
    source=SimpleNamespace(feedback=lambda:(full.copy(),measured.copy(),(clock[0],)*16),
                           state_valid_mask=original.state_valid_mask)
    output=make_output(spec['gate']['config'])
    output_owner=F1Owner(MemoryTransport(),source,numeric.numerics.mapping,output,numeric.numerics.gate,powered=True)
    output_owner.preventive_boundary_recovery=True
    output_owner.initialize(clock[0]/1e9);output_owner.prepare()
    from f1dagger.config import pico_paths
    profile=pico_paths(spec['gate']['config']['_infra_root'])/'config/control.json'
    output_profile=dict(path=str(profile),sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        velocity_deg_s=output.planner.max_velocity,acceleration_deg_s2=output.planner.max_acceleration,
        jerk_deg_s3=output.planner.max_jerk,robot_profile_match_verified=False)
    for module_name in ('f1dagger.robot_io','f1dagger.arm_output','pico_f1.arm_output','pico_f1.trajectory','pico_f1.config'):
        import sys
        module_path=Path(sys.modules[module_name].__file__)
        source_files[str(module_path)]=hashlib.sha256(module_path.read_bytes()).hexdigest()
    output_control_source=digest({name:source_files[str(Path(sys.modules[name].__file__))] for name in
        ('f1dagger.robot_io','f1dagger.arm_output','pico_f1.arm_output','pico_f1.trajectory','pico_f1.config')})
    output_profile['control_source_sha256']=output_control_source
    source_files[str(profile)]=output_profile['sha256']
obj=next(o for o in original.objects if o.object_id==task.object_id)
hole=next(o for o in original.objects if o.object_id==task.hole_id)
def translated(item,offset):
    value=transform(item.pose).copy();value[:3,3]+=np.asarray(offset)/1000.
    return item.model_copy(update=dict(pose=tuple(value.ravel())))
obj=translated(obj,args.object_offset_mm);hole=translated(hole,args.hole_offset_mm)
object_pose=transform(obj.pose);attachment=None;sequence=0;history=[];outcomes=[]
import queue
perception_updates=queue.SimpleQueue()
perception_entities=set() if args.rpc_perception_config else {task.object_id,task.hole_id}
def install_perception_scene(scene):
    done=threading.Event();perception_updates.put((scene,done));return done
simulation_frame_blobs={}
if args.rpc_planner:
    # These are explicit simulated sensor placeholders for archival plumbing,
    # never images passed to SAM or claimed as measured motion camera frames.
    for frame in original.frames:
        if frame.encoding not in ('rgb8','bgr8','16UC1','16uc1','32FC1','32fc1'):
            raise ValueError('unsupported_simulated_frame_encoding')
        size=frame.row_step_bytes*frame.height
        simulation_frame_blobs[(frame.camera,frame.stream)]=store.put_blob(bytes(size))
def pose(q):return transform(numeric.dispatch('forward_pose',dict(full22=full.tolist(),held16=q.tolist())))
recovery_anchor=None;recovery_observed=False;miss_injected=False
aperture_viewpoint = None
occluded_tcp = None
cleared_aperture = False
if args.occlude_aperture_after_transfer:
    from f1_harness.aperture_view import ApertureViewpoint, planner_view_geometry
    from f1_harness.frames import FrameChain
    plan_config = json.loads((site/'plan.local.json').read_text())
    scene_config = json.loads((site/'gateway.local.json').read_text())['scene']
    envelope = planner_view_geometry(plan_config,scene_config)
    if envelope is None:
        raise ValueError('offline_view_requires_registered_motion_geometry')
    aperture_viewpoint = ApertureViewpoint(envelope, FrameChain.load(fixture['frame_chain']), geometry)

def observation():
    global sequence,recovery_anchor,recovery_observed,occluded_tcp,cleared_aperture
    sequence+=1
    returning=engine.status()['perception_context']['placement_verified']
    relative=np.linalg.inv(transform(hole.pose))@object_pose
    yaw=math.atan2(relative[1,0],relative[0,0]);yaw_error=abs((yaw+math.pi/4)%(math.pi/2)-math.pi/4)
    tilt=math.acos(float(np.clip(relative[2,2],-1,1)))
    inserted=bool(np.linalg.norm(relative[:2,3])<=geometry['xy_tolerance_m']
        and insertion_depth_reached(float(relative[2,3]),geometry['insertion_depth_m'],
            tolerance_m=geometry.get('insertion_depth_tolerance_m',0.) if task.admission_profile=='development_coarse' else 0.)
        and yaw_error<=geometry['yaw_tolerance_rad'] and tilt<=geometry['tilt_tolerance_rad'])
    still=bool(len(history)>1 and history[-1][0]-history[0][0]>=300_000_000
        and np.max(np.ptp([v for _,v in history],axis=0))<=.05)
    evidence_data=dict(simulation=True,sequence=sequence,object_pose=object_pose.tolist())
    evidence=((store.put_blob(json.dumps(evidence_data).encode()) if args.rpc_planner else digest(evidence_data)),)
    target=obj.model_copy(update=dict(pose=tuple(object_pose.ravel()),held=attachment is not None,
        inserted=inserted,inserted_hole_id=task.hole_id if inserted else None,stationary=still,
        eef_object_pose=tuple(attachment.ravel()) if attachment is not None else None,evidence_ids=evidence))
    if attachment is not None and args.grasp_observation_evidence!='ideal':
        running=engine.running
        if (args.simulated_gripper_can and args.grasp_observation_evidence=='miss_once_then_observe'
                and not recovery_observed and miss_injected and still and target.visible
                and 'verify_grasp' in engine.completed_skills):
            # A new stationary RGB-D measurement of the regrasped object has
            # its own pose. Reconstruct its transform from measured FK. The
            # independent encoder has already confirmed closure.
            measured_attachment = np.linalg.inv(pose(measured)) @ transform(target.pose)
            target = target.model_copy(update={'eef_object_pose':tuple(measured_attachment.ravel())})
            recovery_observed = True
            injections.append(dict(kind='simulated_attachment_remeasured_after_regrasp',
                                   observation_id=f'simulation:{sequence}',physical_motion=False))
        if running and running.skill=='recover_grasp':
            current=pose(measured)
            if recovery_anchor is None:recovery_anchor=current.copy()
            if np.linalg.norm(current[:3,3]-recovery_anchor[:3,3])>.01:
                recovery_observed=True
        if not recovery_observed or args.grasp_observation_evidence=='always_unknown':
            target=target.model_copy(update=dict(held=None,eef_object_pose=None))
    frames=tuple(frame.model_copy(update=dict(received_ns=clock[0],source_ns=clock[0],sequence=sequence,
        blob_sha256=(simulation_frame_blobs[(frame.camera,frame.stream)] if args.rpc_planner else
                     digest(dict(simulation=True,sequence=sequence,camera=frame.camera,stream=frame.stream)))))
        for frame in original.frames if not returning or frame.camera=='head')
    objects = tuple(o for o in ((hole,) if returning else (target,hole)) if o.object_id in perception_entities)
    eef = None
    if aperture_viewpoint is not None and attachment is not None:
        eef = object_pose @ np.linalg.inv(attachment)
        tcp = (aperture_viewpoint.native_world @ eef @ aperture_viewpoint.envelope['tool'])[:3,3]
        if (occluded_tcp is not None or 'transfer_above_hole' in engine.completed_skills) and not cleared_aperture:
            if occluded_tcp is None:
                occluded_tcp = tcp.copy()
                injections.append(dict(kind='simulated_aperture_hidden',observation_id=f'simulation:{sequence}',
                                       tcp=tcp.tolist(),physical_motion=False))
            delta = tcp - occluded_tcp
            normal = aperture_viewpoint.envelope['normal']
            lateral = np.linalg.norm(delta - normal * (delta @ normal))
            if lateral < aperture_viewpoint.envelope['hole_width']/2:
                objects = tuple(o for o in objects if o.object_id != task.hole_id)
            else:
                cleared_aperture = True
                injections.append(dict(kind='simulated_aperture_visible_after_lateral_displacement',
                    observation_id=f'simulation:{sequence}', displacement_m=float(lateral),physical_motion=False))
        # The offline sensor model owns these placeholder frames. Bind its
        # object measurements to this tick's frame IDs like the real observer.
        ids = tuple(f.blob_sha256 for f in frames if f.camera==original.geometry_camera)
        objects = tuple(o.model_copy(update={'evidence_ids':ids}) for o in objects)
    scene = original.model_copy(update=dict(synthetic=False,observation_id=f'simulation:{sequence}',
        acquisition_instance='offline-simulation',host_boot_id=engine.host_boot_id,clock_domain='virtual_owner',
        received_ns=clock[0],state_received_ns=clock[0],frames=frames,
        pairings=tuple(p for p in original.pairings if not returning or p.camera=='head'),
        scene_version=f'simulation:{sequence}',stationary=still,state16=tuple(measured),
        geometry_scope='coarse_fixture_return' if returning else 'coarse_static_task',
        geometry_camera='head' if returning else original.geometry_camera,
        fixture=original.fixture if perception_entities else None,
        objects=objects))
    if gripper_plant is not None:
        scene=gripper_plant.observe(scene,engine.status()['perception_context'],
            measured_closure=measured[15],blocked=attachment is not None,
            verifier_available=not verifier_dropout.is_set())
    if eef is not None:
        aperture_viewpoint.update(scene,engine.status()['perception_context'],eef)
    return scene

class DeferredExecutor:
    def __init__(self):self.pending=[]
    def submit(self,function,*args):
        future=Future();self.pending.append((future,function,args));return future
    def pump(self):
        jobs,self.pending=self.pending,[]
        for future,function,args in jobs:
            if future.cancelled():continue
            begin=time.perf_counter()
            try:future.set_result(function(*args))
            except Exception as exc:future.set_exception(exc)
            intents=[value for value in args if isinstance(value,SkillIntent)]
            if len(intents)!=1:raise RuntimeError('simulation_computation_intent_ambiguous')
            computations.append(dict(function=function.__name__,wall_s=time.perf_counter()-begin,
                request_id=intents[0].request_id,skill=intents[0].skill))
    def shutdown(self,**kwargs):pass
prefetch=None;prefetch_setup=None;tick_timing=None
def plan(intent,obs,full,held,*,preview=False):
    selection=engine.grasp_selection_for(intent,preview=True) if preview else engine.grasp_selection_for(intent)
    with (run/'planning-inputs.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(simulation=True,intent=intent.model_dump(),observation=obs.model_dump(),
            full22=list(full),held16=list(held),grasp_selection=selection,preview=preview))+ '\n')
    value=prefetch.take(intent,obs,full,held) if prefetch is not None and not preview else None
    if value is None:
        value=service.dispatch('plan_skill',dict(intent=intent.model_dump(),
            observation=obs.model_dump(),full22=full,held16=held,
            grasp_selection=selection,preview=preview))
    if prefetch is not None and not preview:prefetch.start(intent,obs,full,value)
    plans.append(dict(skill=intent.skill,request_id=intent.request_id,proposal=value))
    return JointProposal(**value)
def revalidate(intent,proposal,original,latest,full,held,after):
    return service.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=asdict(proposal),
        original_observation=original.model_dump(),observation=latest.model_dump(),
        full22=full,held16=held,after_ns=after))
def waypoint(intent,proposal,obs,index,full,measured):
    return service.dispatch('check_waypoint',dict(intent=intent.model_dump(),proposal=asdict(proposal),
        observation=obs.model_dump(),index=index,full22=full,measured16=measured))
def verify(intent,after):
    if intent.skill==args.missing_scene_at and len(injections)<args.missing_scene_calls:
        # Simulate loss only on the verifier's scene delivery lane. Owner live
        # geometry remains available. This is NOT a camera/segmentation test.
        if args.wall_clock:verifier_dropout.set()
        with verifier.condition:verifier.latest=None
        injections.append(dict(skill=intent.skill,request_id=intent.request_id,
            verification_request_id=intent.verification_request_id,after_ns=after))
    try:result=verifier.dispatch('verify_skill',dict(intent=intent.model_dump(),after_ns=after, close_request_id=(engine.grasp_contact or {}).get('request_id')))
    finally:verifier_dropout.clear()
    validated=SkillResult.model_validate(result)
    # Exercise the verifier's actual hardware-contract receipt in RPC fixtures.
    # Its inputs remain explicitly simulated in the enclosing report/events;
    # this contract flag is not evidence of a physical grasp or placement.
    return validated if args.rpc_planner else validated.model_copy(update={'synthetic':True})
bridge=OwnerBridge(engine,numerical_planner=plan,result_verifier=verify,candidate_revalidator=revalidate,
    waypoint_verifier=waypoint,clock=lambda:clock[0],feedback_age_ns=200_000_000,
    reference_tolerance_deg=.5,stop_delta_deg=.05,stop_window_ns=300_000_000,stop_gripper_delta=.5)
bridge.preview_planner=lambda intent,obs,full,held:plan(intent,obs,full,held,preview=True)
bridge.executor.shutdown()
paced_clock=None;async_ticks=[];cancel_injected=None;planning_process=None
if args.wall_clock:
    from wall_clock_replay import PacedClock,AsyncReplayExecutor
    paced_clock=PacedClock(origin_ns=args.clock_origin_ns,max_seconds=args.max_wall_seconds)
    bridge.executor=AsyncReplayExecutor(computations,plan_delay_s=args.plan_delay_s)
    p=Path(__file__).with_name('wall_clock_replay.py');source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
    from replay_planning_process import PlanningProcess
    planning_spec=deepcopy(spec);planning_spec['gate']['runtime']=str(area/'planning-native-model')
    planning_process=PlanningProcess(task=task,fixture=fixture,spec=planning_spec,runtime=area/'planning-process',
        clock_mapping=lambda:dict(origin_ns=args.clock_origin_ns,start_wall_ns=paced_clock.start_ns))
    service=planning_process
    for name in ('replay_planning_process.py','audit_worker_launch.py'):
        p=Path(__file__).with_name(name);source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
    if args.prefetch_grasp:
        from replay_grasp_prefetch import GraspProcess,GraspPrefetch
        prefetch_spec=deepcopy(spec);prefetch_spec['gate']['runtime']=str(area/'prefetch-native-model')
        try:
            clock_mapping=lambda:dict(origin_ns=args.clock_origin_ns,start_wall_ns=paced_clock.start_ns)
            if prefetch_factory is None:
                worker_class=GraspProcess
                if args.prefetch_transfer:
                    from replay_transfer_prefetch import NextSkillProcess
                    worker_class=NextSkillProcess
                worker=worker_class(task=task,fixture=fixture,spec=prefetch_spec,runtime=area/'prefetch-process',
                    clock_mapping=clock_mapping)
            else:
                worker,prefetch_setup=prefetch_factory(task=task,fixture=fixture,spec=prefetch_spec,clock_mapping=clock_mapping,
                    scope=dict(task_id=task.task_id,task_hash=digest(task),calibration_id=task.calibration_id,
                        session_id=session,owner_instance_id=engine.instance,epoch=engine.epoch))
        except BaseException:
            planning_process.close();bridge.close()
            bridge.executor.shutdown(wait=True,cancel_futures=True)
            if output_owner is not None:output_owner.close()
            if audit_pool is not None:audit_pool.close()
            raise
        def current(intent):
            return (engine.authorized and not engine.hold_requested and engine.epoch==intent.epoch
                and engine.instance==intent.owner_instance_id and engine.session==intent.session_id
                and digest(engine.task)==intent.task_hash)
        def parent_succeeded(intent):
            result=engine.results.get(intent.request_id)
            return current(intent) and result is not None and result.outcome=='success'
        if worker is not None:prefetch=GraspPrefetch(worker,current,parent_succeeded,delay_s=args.prefetch_delay_s,
            transfer=args.prefetch_transfer,transfer_delay_s=args.transfer_prefetch_delay_s)
        for name in ('replay_grasp_prefetch.py','prepared_grasp.py',*(('prepared_transfer.py','replay_transfer_prefetch.py') if args.prefetch_transfer else ())):
            p=Path(__file__).with_name(name);source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        if prefetch_factory is not None:
            for name in ('warm_grasp_process.py','replay_warm_sequence.py','audit_assets.py','audit_asset_watch.py'):
                p=Path(__file__).with_name(name);source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
else:
    bridge.executor=DeferredExecutor()
def tick():
    global held,measured,attachment,object_pose,history,tick_count,cancel_injected,tick_timing,original,obj,hole,perception_entities,miss_injected
    consumed=[]
    while not perception_updates.empty():
        actual,done=perception_updates.get_nowait()
        if engine.request_publications:raise ValueError('recorded_initial_scene_cannot_replace_motion_scene')
        original=actual
        perception_entities={o.object_id for o in actual.objects}
        for item in actual.objects:
            if item.object_id==task.object_id:obj=item;object_pose=transform(item.pose)
            if item.object_id==task.hole_id:hole=item
        store.event('offline_real_perception_consumed',source_observation_id=actual.observation_id,
            source_received_ns=actual.received_ns,source_scene_blob=store.put_blob(actual.model_dump_json().encode()),
            actuator_feedback='simulated',source_clock_unchanged_in_evidence=True)
        consumed.append(done)
    if paced_clock is None:clock[0]+=args.tick_pattern_ns[tick_count%len(args.tick_pattern_ns)]
    else:clock[0]=paced_clock.advance()
    tick_timing=dict(sample_ns=clock[0],body_start_ns=time.monotonic_ns())
    if prefetch is not None:
        if not engine.authorized or engine.hold_requested:prefetch.invalidate('authority_revoked')
        if args.cancel_during_prefetch and cancel_injected is None:
            with prefetch.lock:slot=prefetch.slot
            if (slot is not None and not slot['future'].done()
                    and engine.request_publications.get(slot['parent'].request_id)):
                cancel_injected=dict(sample_ns=clock[0],wall_ns=time.monotonic_ns(),epoch=engine.epoch,
                    reason='offline_cancel_during_prefetch')
                bridge.revoke(cancel_injected['reason']);prefetch.invalidate(cancel_injected['reason'])
    tick_count+=1
    if not args.rpc_planner:engine.heartbeat(session)
    active=bridge.executor.active_jobs() if paced_clock is not None else []
    if args.cancel_during_plan and cancel_injected is None and any(j['function']=='plan' for j in active):
        cancel_injected=dict(sample_ns=clock[0],wall_ns=time.monotonic_ns(),epoch=engine.epoch,
            jobs=active,reason='offline_cancel_during_plan')
        bridge.revoke(cancel_injected['reason'])
    history.append((clock[0],measured[7:14].copy()))
    # Retain the sample bracketing the window start, as the real scene pipeline
    # does. Dropping every older sample would require an exact timestamp at
    # 300ms and never become stationary under alternating 19/21ms sampling.
    while len(history)>3 and history[1][0]<=clock[0]-300_000_000:history.pop(0)
    obs=observation()
    # The request snapshot is frozen by observe/submit. Ongoing perception
    # updates only the live revalidation lane, as the real gateway does.
    if not args.rpc_planner and engine.pending is None and engine.running is None:
        engine.update_observation(obs)
    engine.update_live_observation(obs,engine.status()['perception_context'])
    for done in consumed:done.set()
    assert engine.mode is (Mode.HARDWARE if args.rpc_planner else Mode.FAKE) and obs.observation_id.startswith('simulation:')
    if not verifier_dropout.is_set():verifier.dispatch('update_scene',dict(observation=obs.model_dump()))
    tick_timing['observed_ns']=time.monotonic_ns()
    bridge.executor.pump()
    full[7:14]=measured[7:14]
    poll_args=dict(full22=tuple(full),measured16=tuple(measured),held16=tuple(held),stamps16=(clock[0],)*16)
    if choices is None:
        target=bridge.poll(**poll_args)
    else:
        # Offline experiment seam: only the reference sampler inside this
        # synchronous FAKE poll is replaced. Production owner code is unchanged.
        with patch('f1_harness.trajectory.sample',
                   lambda *a,**kw:choices.sample_for_bridge(bridge,*a,**kw)):
            target=bridge.poll(**poll_args)
    tick_timing['polled_ns']=time.monotonic_ns()
    if args.preview_segments and target is not None and bridge.proposal is not None:
        proposal=bridge.proposal;running=engine.running
        key=(proposal.request_id,bridge.trajectory_start_index)
        if (running and running.skill in ('transfer_above_hole','withdraw_reset')
                and proposal.trajectory is not None and bridge.waypoint_published_ns is None
                and key not in previewed_segments):
            previewed_segments.add(key)
            # Capture the last completed publication separately from sensors.
            # The owner receipt clock is real; sampling/feedback here is virtual.
            publication=dict(record=deepcopy(output_owner.last_evidence),
                sample_ns=round(output_owner.output.sampled*1e9),sample_clock='fake_owner_clock')
            feedback=dict(measured16=measured.tolist(),source_stamps_ns=list(poll_args['stamps16']),
                received_ns=clock[0],sample_clock='fake_owner_clock')
            forecast=preview_segment(output_owner.output,skill=running.skill,request_id=proposal.request_id,
                trajectory=proposal.trajectory,targets=proposal.targets,start_index=key[1],
                publication=publication,feedback=feedback,first_publication_ns=clock[0],
                post_boundary_samples=args.preview_hold_samples)
            # Diagnostic only: check this forward forecast before the source
            # owner publishes its first reference, then continue the simulation
            # to compare with the actual future. Never turn it into authority.
            from output_sweeps import sweep_outputs
            begin=time.perf_counter()
            forecast['scene_audit']=sweep_outputs(forecast['rows'],[dict(
                kind='harness_planning_started',intent=running.model_dump(),
                observation=bridge.plan_observation.model_dump(),
                full_reference=bridge.plan_full_reference,held_reference=bridge.plan_held_reference)],
                service,numeric)
            forecast['scene_audit_compute_s']=time.perf_counter()-begin
            forecast['observed_output_count_at_creation']=len(owner_outputs)
            forecast['audit_before_first_segment_reference']=True
            segment_previews.append(forecast)
            if args.recommend_segment_speeds is not None:
                def candidate_context():
                    from f1_harness.planning_lessons import planning_scope
                    return dict(session_id=session,owner_instance_id=engine.instance,task_hash=digest(task),
                        epoch=engine.epoch,calibration_id=task.calibration_id,
                        request_id=None if engine.running is None else engine.running.request_id,
                        source_plan_sha256=digest(None if bridge.proposal is None else asdict(bridge.proposal)),
                        planning_scope=planning_scope(task,fixture,running.skill),
                        observation=bridge.plan_observation.model_dump(),
                        full_reference=bridge.plan_full_reference,held_reference=bridge.plan_held_reference,
                        control_profile_sha256=output_profile['sha256'],
                        control_source_sha256=output_control_source,
                        numerical_asset_id=None if audit_pool is None else audit_pool.asset_id)
                event=dict(kind='harness_planning_started',intent=running.model_dump(),
                    observation=bridge.plan_observation.model_dump(),
                    full_reference=bridge.plan_full_reference,held_reference=bridge.plan_held_reference)
                binding=state_binding(output_owner.output,candidate_context())
                def candidate_cancelled():
                    return (engine.hold_requested or engine.running is None
                            or engine.running.request_id!=proposal.request_id)
                forecast_kwargs=dict(skill=running.skill,request_id=proposal.request_id,
                    trajectory=proposal.trajectory,targets=proposal.targets,start_index=key[1],
                    publication=publication,feedback=feedback,first_publication_ns=clock[0],
                    post_boundary_samples=args.preview_hold_samples)
                recommendation=recommend_segment(output_owner.output,context=candidate_context(),
                    current_context=candidate_context,
                    cancelled=candidate_cancelled,
                    forecast_kwargs=forecast_kwargs,
                    geometry_trace=proposal.geometry_trace,reference_speeds=args.recommend_segment_speeds,
                    preferences=segment_preferences,
                    audit=lambda candidate:audit_candidate(candidate,event,service,numeric,
                        pool=audit_pool,context_binding=binding,cancelled=candidate_cancelled,
                        current=lambda:state_binding(output_owner.output,candidate_context())==binding))
                recommendation.update(skill=running.skill,start_index=key[1],
                    observed_output_count_at_creation=len(owner_outputs),consumed_by_controller=False)
                if choices is not None:
                    choice=choices.install(output=output_owner.output,context=candidate_context(),
                        current_context=candidate_context,cancelled=candidate_cancelled,
                        forecast_kwargs=forecast_kwargs,geometry_trace=proposal.geometry_trace,
                        reference_speeds=args.recommend_segment_speeds,recommendation=recommendation)
                    selected=choices.sample_for_bridge(bridge,proposal.trajectory,proposal.targets,
                        start_index=key[1],elapsed_s=0.)[0]
                    if not np.array_equal(selected,target):raise RuntimeError('first_anchor_changed_by_choice')
                    recommendation.update(consumed_in_simulation=True,choice=choice)
                    store.event('simulated_reference_choice',**choice)
                segment_recommendations.append(recommendation)
    if target is not None:
        publications.append(dict(request_id=engine.running.request_id if engine.running else None,
            virtual_ns=clock[0],target16=list(target),simulation=True))
        numeric.numerics.mapping.check_action(target)
        numeric.numerics.gate.check_preventive(target,full)
    command=held.copy() if target is None else np.array(target)
    publication_complete=True;publication_id=f'sim:{clock[0]}'
    if output_owner is not None:
        if engine.hold_requested and not getattr(tick,'holding',False):output_owner.hold(clock[0]/1e9)
        tick.holding=engine.hold_requested
        output_owner.approve(command,clock[0]/1e9,enabled=(False,target is not None))
        command,_,record=output_owner.publish(clock[0]/1e9)
        publication_complete=record['publication_complete'];publication_id=f'sim-owner:{output_owner.sequence}'
        owner_outputs.append(dict(virtual_ns=clock[0],request_id=engine.running.request_id if engine.running else None,
            reference16=output_owner.selected.tolist(),submitted16=command.tolist(),measured16=measured.tolist(),
            new_motion_reference=target is not None,simulation=True,output_evidence=record))
    if command[15]==fixture['grasp_opening'] and held[15]<command[15] and attachment is None:
        if args.grasp_observation_evidence=='miss_once_then_observe' and not miss_injected:
            # Explicit offline plant fault: the first closure leaves the real
            # initial object at its simulated support. Owner miss/hold/recovery
            # logic is unmodified; later attachment is also only a simulation.
            miss_injected=True
            injections.append(dict(kind='simulated_first_closure_miss',virtual_ns=clock[0],
                request_id=engine.running.request_id if engine.running else None,
                physical_motion=False))
        else:attachment=np.linalg.inv(pose(command))@object_pose
    if command[15]<held[15] and attachment is not None:
        object_pose=pose(command)@attachment;attachment=None
    held=np.array(command)
    bridge.after_publication(publication_id=publication_id,measured16=tuple(measured),
        publication_complete=publication_complete,now_ns=clock[0])
    measured=held.copy()  # Ideal actuator response after the optional real software output.
    if attachment is not None:object_pose=pose(measured)@attachment
    tick_timing['body_end_ns']=time.monotonic_ns()
    if paced_clock is not None:
        async_ticks.append(dict(sample_ns=clock[0],wall_ns=time.monotonic_ns(),active_jobs=active,
            authorized=engine.authorized,epoch=engine.epoch,has_motion_reference=target is not None,
            publication_complete=publication_complete,timing=dict(tick_timing)))

class Robot:
    def invoke(self,name,**extra):
        for _ in range(100):
            tick()
            if engine.observation.stationary and not engine.hold_requested:break
        intent=SkillIntent(request_id=uuid.uuid4().hex,session_id=session,owner_instance_id=engine.instance,
            task_id=task.task_id,task_hash=digest(task),epoch=engine.epoch,
            observation_id=engine.observation.observation_id,scene_version=engine.observation.scene_version,
            calibration_id=task.calibration_id,skill=name,budget_ms=300000,**extra)
        begin=clock[0];wall=time.perf_counter();engine.submit_skill(intent.model_dump())
        for _ in range(15010):
            tick();result=engine.skill_result(session,intent.request_id)
            if result['lifecycle'] not in ('accepted','running','paused') and (not engine.hold_requested or args.wall_clock):break
        else:raise RuntimeError('simulation_task_did_not_terminate')
        record=dict(skill=name,result=result,request=intent.model_dump(),virtual_s=(clock[0]-begin)/1e9,wall_s=time.perf_counter()-wall,
                    hole_object=(np.linalg.inv(transform(hole.pose))@object_pose).tolist())
        outcomes.append(record);print(name,result['outcome'],result['reason'],record['virtual_s'],flush=True)
        return result
    def skill(self,name,**parameters):return self.invoke(name,parameters=parameters)
    def resume(self,name,request):return self.invoke(name,resume_request_id=request)
    def recheck(self,name,request):return self.invoke(name,verification_request_id=request)
result=None;error=None
from replay_gc_watch import ReplayGCWatch
gc_watch=ReplayGCWatch();gc_watch.start()
import gc
gc_enabled=gc.isenabled()
if args.defer_fixture_cyclic_gc:gc.disable()
p=Path(__file__).with_name('replay_gc_watch.py');source_files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
try:
    if args.rpc_planner:
        from replay_rpc_session import run_rpc_session
        result=run_rpc_session(globals())
    elif args.program=='registered':
        from f1_harness.recovery_programs import compile_library
        program=compile_library(task.local_corrections,task.regrasp_limit,
            grasp_observation_enabled='recover_grasp' in task.allowed_skills)+'\nresult = pick_and_place(**APPROACH_PARAMETERS)\n'
    else:program=single_block_program(task)
    if not args.rpc_planner:
        namespace={'robo':Robot(),'APPROACH_PARAMETERS':approach_parameters};exec(program,namespace);result=namespace['result']
    if cancel_injected is not None:
        # Observe the late in-flight result while ticks continue. Revocation
        # must prevent publication; this does not claim physical hold feedback.
        while bridge.future is not None and not bridge.future.done():tick()
        if args.cancel_during_prefetch:
            while prefetch.future is not None and not prefetch.future.done():tick()
        for _ in range(3):tick()
except Exception as exc:
    error=dict(type=type(exc).__name__,message=str(exc));print(error,flush=True)
finally:
    if args.defer_fixture_cyclic_gc and gc_enabled:gc.enable()
    gc_watch.close()
    final=engine.status();bridge.close()
    if prefetch is not None:prefetch.close()
    if planning_process is not None:planning_process.close()
    if paced_clock is not None:bridge.executor.shutdown(wait=True,cancel_futures=True)
    if output_owner is not None:output_owner.close()
    if audit_pool is not None:audit_pool.close()
    if numerics_ipc_server is not None:numerics_ipc_server.close()
provenance=replay_provenance(engine.mode.value,outcomes,
    [json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()],
    publications,owner_outputs)
if args.rpc_planner:
    provenance['scope_checks']['fake_engine']=False
    provenance['scope_checks']['local_hardware_contract_memory_fixture']=bool(
        engine.recording_identity.get('offline_fixture') and type(output_owner.transport).__name__=='MemoryTransport')
    provenance['simulation_scope_confirmed']=all(v for k,v in provenance['scope_checks'].items() if k!='fake_engine')
report=dict(schema='f1-full-owner-simulation-v1',physical_motion=False,robot_connected=False,paid_model_calls=0,
    script_sha256=__import__('hashlib').sha256(Path(__file__).read_bytes()).hexdigest(),
    scenario=json.loads(json.dumps(vars(args),default=lambda value:str(value) if isinstance(value,Path)
        else (_ for _ in ()).throw(TypeError(type(value).__name__)))),injections=injections,
    output_profile=output_profile,
    offline_relocations=dict(frame_owner_original=frame_owner_original,
        frame_owner_local=fixture['frame_chain'].get('development_owner_config'),numerics_spec=spec),
    source_scene_file=str(source_path),source_scene_sha256=__import__('hashlib').sha256(source_path.read_bytes()).hexdigest(),
    run=str(run),mode=engine.mode.value,all_results_synthetic=provenance['all_results_synthetic'],
    simulation_provenance=provenance,all_geometry_observations_are_mock_inputs=True,
    result=result,error=error,stages=outcomes,computations=computations,final_status=final,
    completed_skills=sorted(engine.completed_skills),
    attachment_evidence_injection=args.grasp_observation_evidence,
    source_files=source_files,
    fixture_cyclic_gc_deferred=args.defer_fixture_cyclic_gc,
    virtual_s=(clock[0]-args.clock_origin_ns)/1e9,wall_s=time.perf_counter()-started,
    limitations=['Hardware-like mock geometry uses synthetic=False to exercise unmodified validators; it is not acquisition evidence.',
        'Ideal actuator following after the chosen output implementation, rigid grasp; no force/drop/occlusion/noise model.',
        'Clock pauses during synchronous numeric work; virtual cycle is not a wall-clock performance claim.',
        'No production camera pipeline, RPC, ROS or physical plant; software smoothing only when follower=approved-output.',
        'Approved-output uses the local profile with memory transport; matching the current robot profile is unverified.',
        'Synthetic success cannot be promoted as real recovery experience or physical acceptance.'])
if args.rpc_planner:
    report['offline_hardware_contract_fixture']=dict(actuator_transport='MemoryTransport',
        rpc='actual RPent HTTP + Unix owner mailbox',worker='actual bwrap PyRUA',
        model='scripted program' if args.rpc_planner=='recorded' else 'actual ResponsesPlanner',
        perception='saved real initial scene; simulated following observations and placeholder raw blobs')
    if args.rpc_perception_config:
        report['offline_hardware_contract_fixture']['perception']='Actual tools/SAM on request-paced original RGB-D before motion; geometry consumed by simulated actuator contract, following observations simulated'
        report['actual_initial_perception_report']=str(run/'perception/report.json')
    report['limitations'][3]='Actual localhost RPent/Unix RPC and bwrap worker; no live camera pipeline, ROS or physical plant.'
    model_trace=run/'planner/trace.jsonl'
    report['paid_model_calls']=sum(json.loads(l).get('kind')=='model_request_started'
        for l in model_trace.read_text().splitlines()) if model_trace.exists() else 0
(run/'mock-publications.json').write_text(json.dumps(publications,indent=2)+'\n')
(run/'plans.json').write_text(json.dumps(plans,indent=2)+'\n')
(run/'mock-owner-outputs.json').write_text(json.dumps(owner_outputs,indent=2)+'\n')
if prefetch_setup is not None:report['prewarmed_grasp_setup']=prefetch_setup
if prefetch is not None:
    (run/'prefetch.json').write_text(json.dumps(prefetch.records,indent=2)+'\n')
    report['grasp_prefetch']=dict(enabled=True,records=str(run/'prefetch.json'),
        transfer_enabled=args.prefetch_transfer,
        planner_startup_s=prefetch.worker.startup_s,planner_cleanup=prefetch.worker.cleanup,
        adopted=sum(r['kind']=='adopted' for r in prefetch.records),
        fallback=sum(r['kind']=='fallback' for r in prefetch.records),
        physical_motion=False,production_enabled=False)
if paced_clock is not None:
    (run/'async-ticks.json').write_text(json.dumps(async_ticks,indent=2)+'\n')
    (run/'gc-events.json').write_text(json.dumps(dict(rows=gc_watch.rows,dropped=gc_watch.dropped),indent=2)+'\n')
    report['wall_clock_replay']=dict(clock_origin_ns=args.clock_origin_ns,
        start_wall_ns=paced_clock.start_ns,intervals_ns=paced_clock.intervals_ns,
        ready_to_done_s=(clock[0]-args.clock_origin_ns)/1e9,
        process_entry_to_report_s=(time.monotonic_ns()-PROCESS_ENTRY_NS)/1e9,
        cancellation=cancel_injected,control_and_planner_models_separate=planning_process is not None,
        planner_process=dict(pid=planning_process.process.pid,startup_s=planning_process.startup_s,
            initialization_sha256=planning_process.initialization_sha256,cleanup=planning_process.cleanup),
        candidate_optimizer_enabled=False,wall_time_includes_worker_computation=True,
        ticks_during_computation=sum(bool(r['active_jobs']) for r in async_ticks))
    report['wall_clock_replay']['last_tick_timing']=tick_timing
    report['limitations'][2]='Wall-clock advances during computation; local threaded scheduling with ideal plant, not hardware latency.'
if args.preview_segments:
    (run/'segment-previews.json').write_text(json.dumps(segment_previews,indent=2)+'\n')
    report['segment_previews']=dict(path=str(run/'segment-previews.json'),count=len(segment_previews),
        compute_s=sum(p['compute_s'] for p in segment_previews),
        scene_audit_compute_s=sum(p['scene_audit_compute_s'] for p in segment_previews),
        execution_effect=False)
if args.recommend_segment_speeds is not None:
    (run/'segment-recommendations.json').write_text(json.dumps(segment_recommendations,indent=2)+'\n')
    report['segment_recommendations']=dict(path=str(run/'segment-recommendations.json'),
        count=len(segment_recommendations),execution_effect=False,
        preference_snapshot=preference_snapshot,
        audit_workers=None if audit_pool is None else dict(count=args.candidate_audit_workers,
            startup_s=audit_pool.startup_s,cleanup=audit_pool.last_cleanup,asset_id=audit_pool.asset_id,
            asset_validation_mode=audit_pool.asset_validation_mode),
        compute_s=sum(item['elapsed_s'] for item in segment_recommendations))
if choices is not None:
    from output_sweeps import sweep_outputs
    events=[json.loads(line) for line in (store.root/'trace.jsonl').read_text().splitlines()]
    actual_audit=sweep_outputs(owner_outputs,events,service,numeric)
    (run/'consumed-segments.json').write_text(json.dumps(choices.receipts,indent=2)+'\n')
    (run/'consumed-forecasts.json').write_text(json.dumps(
        [entry['forecast'] for entry in choices.choices.values()],indent=2)+'\n')
    (run/'actual-output-audit.json').write_text(json.dumps(actual_audit,indent=2)+'\n')
    report['simulated_consumption']=dict(count=len(choices.receipts),production_authorized=False,
        original_geometric_proposals_unchanged=True,actual_output_audit=actual_audit,
        limitation='Reference sampler replaced only inside FAKE replay poll; not a live owner/IPC integration.')
checks=dict(no_uncaught_error=error is None,expected_outcome=bool(result and result['outcome']==args.expect_outcome),
    simulation_scope_confirmed=provenance['simulation_scope_confirmed'],
    source_unchanged=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in source_files.items()))
rechecks=[s['request']['request_id'] for s in outcomes if s['request'].get('verification_request_id')]
checks['rechecks_never_publish']=not any(p['request_id'] in rechecks for p in publications)
checks['rechecks_never_replan']=not any(c['request_id'] in rechecks and c['function']=='plan' for c in computations)
checks['verification_calls_bounded']=all(sum(c['request_id']==s['request']['request_id'] and c['function']=='verify'
    for c in computations)<=2 for s in outcomes)
if cancel_injected is not None:
    checks['no_motion_after_injected_cancel']=not any(p['virtual_ns']>=cancel_injected['sample_ns'] for p in publications)
    if args.cancel_during_prefetch:
        checks['late_prefetch_completed_after_cancel']=any(r['kind']=='prepare_finished'
            and r['wall_ns']>cancel_injected['wall_ns'] for r in prefetch.records)
        checks['cancelled_prefetch_never_adopted']=not any(r['kind']=='adopted' for r in prefetch.records)
    else:
        checks['late_plan_completed_after_cancel']=any(c['function']=='plan' and c['ended_wall_ns']>cancel_injected['wall_ns'] for c in computations)
if choices is not None:
    checks['selected_output_sweeps_clear']=bool(actual_audit) and all(a['passed'] for a in actual_audit)
    checks['selected_segments_actually_sampled']=bool(choices.receipts) and all(c['sampled_count']>1 for c in choices.receipts)
if result and result['outcome']=='success':
    required={'approach_grasp','grasp','verify_grasp','inspect_in_hand','transfer_above_hole',
              'align','insert','release','verify_placed','withdraw_reset'}
    if args.occlude_aperture_after_transfer:
        # move_tcp invalidates alignment/transport readiness. The successful
        # transport receipt remains part of the same session's execution history.
        completed=[s['skill'] for s in outcomes if s['result']['outcome']=='success' and
                   s['request']['session_id']==session and s['request']['epoch']==outcomes[-1]['request']['epoch']]
        suffix=['move_tcp','reobserve','align','insert','release','verify_placed','withdraw_reset']
        checks['full_cycle_completed']=required.issubset(completed) and completed[-len(suffix):]==suffix
        checks['occlusion_cleared_by_displacement']=cleared_aperture and [
            i['kind'] for i in injections if i['kind'].startswith('simulated_aperture_')]==[
            'simulated_aperture_hidden','simulated_aperture_visible_after_lateral_displacement']
    elif args.simulated_gripper_can and args.grasp_observation_evidence=='miss_once_then_observe':
        # "regrasp" replaces the failed first physical grasp in this scenario.
        completed = {s['skill'] for s in outcomes if s['result']['outcome']=='success' and
                     s['request']['session_id']==session and s['request']['epoch']==outcomes[-1]['request']['epoch']}
        checks['full_cycle_completed']=(required-{'grasp'}|{'regrasp'}).issubset(completed)
        checks['miss_then_remeasured_regrasp'] = [i['kind'] for i in injections]==[
            'simulated_first_closure_miss','simulated_attachment_remeasured_after_regrasp']
    else:
        checks['full_cycle_completed']=required<=engine.completed_skills
report['checks']=checks
(run/'report.json').write_text(json.dumps(report,indent=2)+'\n')
(area/'latest-report.json').write_text(json.dumps(dict(path=str(run/'report.json')),indent=2)+'\n')
print('report',run/'report.json',flush=True)
if not all(checks.values()):raise SystemExit('offline replay assertions failed: '+repr(checks))
