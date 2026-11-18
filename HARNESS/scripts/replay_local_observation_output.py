"""Real native/F1Owner/Ruckig output into memory; no ROS or robot connection.

Original RGB-D/SAM/encoder input is immutable. Subsequent encoder response is
an explicitly ideal offline actuator; no future image or visual improvement
is generated. This isolates a world-move primitive, not a full autonomous task.
"""
import argparse,hashlib,json,time
from pathlib import Path
from types import SimpleNamespace
import numpy as np

from f1_harness.contracts import Observation,Task,SkillIntent,digest
from f1_harness.frames import FrameChain
from f1_harness.geometry import transform
from f1_harness.local_environment import local_sweep_evidence,require_departure_endpoint,seal_sweep_certificate,check_sweep_certificate
from f1_harness.motion_goals import apply_request
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from f1_harness.skills import _motion_clear
from ordinary_output_sweeps import local_endpoint_padding_status
from f1_harness.trajectory import sample
from f1dagger.robot_io import F1Owner,make_output
from dataclasses import replace

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--root',type=Path,required=True);p.add_argument('--site',type=Path,required=True)
p.add_argument('--input',type=Path,required=True);p.add_argument('--reference-report',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--rotation-deg',type=float,nargs=3,default=[0.,0.,0.],help='Diagnostic world rotation of the proposed observation target')
p.add_argument('--screen',action='store_true',help='Short output prefix diagnostic; never complete-motion acceptance')
p.add_argument('--geometry-only-source',action='store_true',help='Explicit development diagnostic; source-admission validation not exercised')
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
source=json.loads(a.input.read_text());obs=Observation.model_validate(source['output_observation'])
full_initial=np.asarray(source['kinematics']['full22']);task=Task.model_validate_json((a.site/'task.local.json').read_text())
fixture=json.loads((a.site/'plan.local.json').read_text())['fixture']
saved=json.loads(a.reference_report.read_text());spec=saved['offline_relocations']['numerics_spec']
numeric=NumericalDispatcher(spec)
class Direct:
    def call(self,method,**kwargs):return numeric.dispatch(method,kwargs)
if a.geometry_only_source:
    from f1_harness.candidate_failures import CandidateFailures
    service=PlanService.__new__(PlanService);service.task=task;service.fixture=fixture;service.clock=time.monotonic_ns
    chain=fixture['frame_chain'];service.frames=FrameChain(transform(chain['T_ik_world_native']),
        transform(chain['T_ik_eef_native_eef']),chain['native_frame'],chain['ik_world_frame'])
    service.candidate_failures=CandidateFailures();service.posture_prior=fixture.get('success_posture_prior')
else:
    fixture['frame_chain']['development_owner_config']=str(a.site/'owner.local.json')
    service=PlanService(task,fixture,a.output/'unused-local-numerics.sock')
service.numerics=Direct()
base=transform(numeric.dispatch('forward_pose',dict(full22=full_initial.tolist(),held16=list(obs.state16))))
native=np.linalg.inv(service.frames.world_native);tip=transform(fixture['eef_tip_reference'])
tcp=native@base@tip;goal=tcp.copy();goal[2,3]+=.020
from scipy.spatial.transform import Rotation
goal[:3,:3]=Rotation.from_rotvec(np.deg2rad(a.rotation_deg)).as_matrix()@goal[:3,:3]
intent=SkillIntent(request_id='offline-local-observation',session_id='offline-memory',owner_instance_id='offline-memory',
    task_id=task.task_id,task_hash=digest(task),epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,
    calibration_id=obs.calibration_id,skill='move_tcp',budget_ms=30000,
    parameters=dict(target_frame='world',target_tcp_pose=tuple(goal.ravel())))
start=time.perf_counter();plan=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
    full22=full_initial.tolist(),held16=list(obs.state16)));planning_s=time.perf_counter()-start
scene=apply_request(service,service._scene(obs,full_initial.tolist(),obs.state16,skill='move_tcp'),intent)
from f1_harness.revalidation import tolerances
tolerance=tolerances(fixture,'move_tcp')
collector=dict(views={},view_bounds={},occupied=set(),departures=[],object_local_box=fixture['object_local_box'],
    target_motion_tolerance_m=tolerance['position_m'],target_rotation_tolerance_rad=tolerance['rotation_rad'],
    tool_local_boxes=scene.tool_local_boxes,eef_tip_reference=scene.eef_tip_reference,attached_radius_m=scene.attached_radius_m)
proof_scene=replace(scene,local_sweep_certificate=collector)
poses=numeric.dispatch('forward_path',dict(full22=full_initial.tolist(),targets=plan['targets']))
before=base;proof_started=time.perf_counter()
for pose in poses:
    pose=transform(pose)
    if not _motion_clear(proof_scene,before,pose):raise RuntimeError('actual_native_path_failed')
    before=pose
require_departure_endpoint(proof_scene,before)
certificate=seal_sweep_certificate(collector,obs);proof_s=time.perf_counter()-proof_started

class MemoryTransport:
    rmw_gids={key:'offline-memory' for key in ('joint_command','left_gripper_command','right_gripper_command')}
    own_gids={}
    def __init__(self):self.messages=0
    def prepare(self,key):pass
    def assert_exclusive_cached(self,key):pass
    def publish(self,key,value):self.messages+=1
    def release(self,key):pass

def run_output(name,cancel_after=None):
    full=full_initial.copy();measured=np.asarray(obs.state16);transport=MemoryTransport()
    feedback=SimpleNamespace(feedback=lambda:(full.copy(),measured.copy(),(time.monotonic_ns(),)*16),state_valid_mask=obs.state_valid_mask)
    output=make_output(spec['gate']['config']);owner=F1Owner(transport,feedback,numeric.numerics.mapping,
        output,numeric.numerics.gate,powered=True)
    owner.preventive_boundary_recovery=True;owner.initialize(time.monotonic());owner.prepare()
    warmup_started=time.monotonic();warmup_tick=warmup_started
    while time.monotonic()-warmup_started<plan['settle_ns']/1e9:
        now=time.monotonic();owner.approve(measured,now,enabled=(False,False))
        command,_,_=owner.publish(now);measured=np.asarray(command).copy();full[7:14]=measured[7:14]
        warmup_tick+=.020;time.sleep(max(0.,warmup_tick-time.monotonic()))
    rows=[];cancelled=False;started=time.monotonic();next_tick=started
    duration=.3 if a.screen else 3.0
    try:
        while time.monotonic()-started<duration:
            now=time.monotonic();elapsed=now-started
            if cancel_after is not None and elapsed>=cancel_after and not cancelled:
                owner.hold(now);cancelled=True
            # OwnerBridge starts this clock only after the first complete
            # publication: that first timed reference is exactly its anchor.
            reference,index,boundary,_=sample(plan['trajectory'],plan['targets'],start_index=0,elapsed_s=elapsed if rows else 0.)
            if cancelled:reference=measured.copy()
            owner.approve(reference,now,enabled=(False,not cancelled))
            command,_,receipt=owner.publish(now)
            rows.append(dict(sample_ns=round(now*1e9),elapsed_s=elapsed,reference16=owner.selected.tolist(),
                submitted16=np.asarray(command).tolist(),measured16=measured.tolist(),
                output_evidence=receipt,new_motion_reference=not cancelled,cancelled=cancelled,
                request_id=intent.request_id,virtual_ns=round(now*1e9),simulation=True))
            measured=np.asarray(command).copy();full[7:14]=measured[7:14]
            next_tick+=.020;time.sleep(max(0.,next_tick-time.monotonic()))
    finally:owner.close()
    output_path=a.output/(name+'-outputs.json');output_path.write_text(json.dumps(rows,indent=2)+'\n')
    failures=[];previous_q=np.asarray(obs.state16);previous=base;sweep_times=[];samples=0
    for row in rows:
        target=np.asarray(row['submitted16']);steps=max(1,int(np.ceil(np.max(abs(target[:14]-previous_q[:14]))/.1)))
        for fraction in np.linspace(0,1,steps+1)[1:]:
            q=previous_q+fraction*(target-previous_q)
            numeric.numerics.mapping.check_action(q);numeric.numerics.gate.check_preventive(q,full_initial)
            pose=transform(numeric.dispatch('forward_pose',dict(full22=full_initial.tolist(),held16=q.tolist())))
            began=time.perf_counter();clear=_motion_clear(scene,previous,pose);sweep_times.append(time.perf_counter()-began)
            if not clear:failures.append(dict(row=len(sweep_times)-1,elapsed_s=row['elapsed_s'],kind='environment',
                local=local_sweep_evidence(scene,previous,pose)))
            previous=pose;samples+=1
        previous_q=target
    endpoint=None
    if not cancelled and not a.screen:
        try:require_departure_endpoint(scene,previous);endpoint=True
        except Exception as exc:endpoint=False;failures.append(dict(kind='endpoint',reason=str(exc)))
    final_tcp=native@previous@tip
    eef_camera=transform(json.loads((a.site/'gateway.local.json').read_text())['scene']['geometry']['eef_camera'])
    camera_start=native@base@eef_camera;camera_end=native@previous@eef_camera
    result=dict(name=name,passed=not failures and (cancelled or endpoint or a.screen),physical_motion=False,publishers=0,
        actuator_transport='MemoryTransport',actual_f1_owner=True,actual_ruckig=True,ideal_actuator_feedback=True,
        output_rows=len(rows),audit_samples=samples,wall_elapsed_s=time.monotonic()-started,
        max_output_interval_ms=max(np.diff([r['sample_ns'] for r in rows]))/1e6,
        failures=failures[:12],local_endpoint_contract_satisfied=endpoint,
        padding_diagnostic=local_endpoint_padding_status(scene,previous),prefix_only=cancelled or a.screen,
        stopped_without_replaying=cancelled and not any(r['new_motion_reference'] for r in rows if r['cancelled']),
        actual_tcp_translation_m=float(np.linalg.norm(final_tcp[:3,3]-tcp[:3,3])),
        actual_camera_translation_m=float(np.linalg.norm(camera_end[:3,3]-camera_start[:3,3])),
        local_sweep_ms=dict(max=max(sweep_times)*1000,mean=float(np.mean(sweep_times))*1000),
        path=str(output_path),sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
        final_tcp_pose=final_tcp.ravel().tolist())
    (a.output/(name+'-audit.json')).write_text(json.dumps(result,indent=2)+'\n')
    return result

normal=run_output('complete');stopped=None if a.screen else run_output('interrupted',cancel_after=.12)
timings=[]
for _ in range(40):
    begun=time.perf_counter();check_sweep_certificate(certificate,obs,obs);timings.append((time.perf_counter()-begun)*1000)
fixture_out=dict(observation=obs.model_dump(),intent=intent.model_dump(),full22=full_initial.tolist(),
    plan=plan,scene={key:(value.tolist() if isinstance(value,np.ndarray) else value)
        for key,value in scene.__dict__.items() if key not in ('local_collision_evidence','local_sweep_certificate')},
    certificate=certificate)
(a.output/'local-motion-fixture.json').write_text(json.dumps(fixture_out,indent=2)+'\n')
report=dict(schema='f1-local-observation-output-replay-v1',passed=normal['passed'] and (stopped is None or stopped['passed']),
    physical_motion=False,publishers=0,live_astra=False,real_experience_eligible=False,
    original_timestamps_preserved=True,natural_visual_failure=source.get('natural_visual_failure'),
    precise_pose_intentionally_withheld=source.get('precise_pose_intentionally_withheld'),
    source_admission_exercised=not a.geometry_only_source,
    source_input=str(a.input.resolve()),source_input_sha256=hashlib.sha256(a.input.read_bytes()).hexdigest(),
    root=str(a.root.resolve()),planning_s=planning_s,actual_fk_revalidation_s=proof_s,
    owner_certificate_ms=dict(max=max(timings),mean=float(np.mean(timings))),cases=[normal]+([] if stopped is None else [stopped]),
    diagnostic_rotation_deg=a.rotation_deg,prefix_screen_only=a.screen,
    limitations=['Isolated motion primitive; not a complete autonomous task or physical robot trial.',
        'Only initial RGB-D/SAM/encoder evidence is recorded reality; subsequent actuator following is ideal simulation.',
        'No post-move image or vision improvement is invented. STOP is a distinct partial execution, never replayed.',
        'This fixture does not itself exercise host RPC or post-planning acquisition; those have separate integration tests.'])
(a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
