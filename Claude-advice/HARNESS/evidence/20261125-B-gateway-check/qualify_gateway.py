"""Exercise the installed gateway over its task RPC with passive robot feedback.

Run on ARM after sourcing the proposed production ROS environment, with the production
SAM reverse tunnel active. Native numerics, verifier, and gateway run separately with
private transport addresses. The feedback IPC explicitly denies all control methods.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen


def read(path):
    return json.loads(path.read_bytes())


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def hashfile(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scene_metrics(obs, matches, config):
    by_camera = {}
    for camera in ('head', 'right'):
        frames = [f for f in obs['frames'] if f['camera'] == camera and f['stream'] in ('rgb', 'depth')]
        streams = {f['stream']: f for f in frames}
        pair = next((p for p in obs.get('pairings', []) if p['camera'] == camera), None)
        times = [f['received_ns'] for f in frames]
        target = (min(times) + max(times)) // 2 if times else None
        feedback = matches.get(target)
        matched_ns = feedback['state_ns'] if feedback else None
        skew_ms = max(abs(t-matched_ns) for t in times) / 1e6 if matched_ns is not None else None
        ok = bool(set(streams) == {'rgb', 'depth'} and pair and pair['quality'] != 'invalid'
                  and streams['rgb']['sequence'] == pair['rgb_sequence']
                  and streams['depth']['sequence'] == pair['depth_sequence']
                  and all(f.get('camera_info_sha256') and f.get('blob_sha256') for f in frames)
                  and skew_ms is not None and skew_ms <= config['frame_state_skew_ms'])
        by_camera[camera] = dict(good=ok, pair=pair, matched_feedback_ns=matched_ns, skew_ms=skew_ms,
                                 frames=[{k:f.get(k) for k in ('stream','sequence','source_ns',
                                          'received_ns','blob_sha256','camera_info_sha256')} for f in frames])
    geometry = config['geometry']
    def measured(object_id):
        obj = next((x for x in obs['objects'] if x['object_id'] == object_id), {})
        pose = obj.get('pose')
        if (not obj.get('visible') or obj.get('frame_id') != geometry['native_frame']
                or obj.get('shape') != 'square' or not obj.get('evidence_ids')
                or not pose or len(pose) != 16 or not all(math.isfinite(x) for x in pose)):
            return None
        return {k:obj[k] for k in ('object_id','frame_id','pose','evidence_ids','shape','visible')}
    block, hole = measured(geometry['object_id']), measured(geometry['hole_id'])
    fixture = obs.get('fixture') or {}
    measured_fixture = bool(fixture.get('source') == 'rgbd_anchor_registration' and fixture.get('evidence_ids')
        and len(fixture.get('pose', [])) == 16 and all(math.isfinite(x) for x in fixture['pose']))
    valid = obs.get('state_valid_mask') or []
    base_ok = (not obs.get('synthetic', True) and len(valid) == 16 and all(valid[7:14])
               and obs.get('calibration_id') == geometry['calibration_id'] and all(p['good'] for p in by_camera.values()))
    return dict(observation_id=obs['observation_id'], received_ns=obs['received_ns'], cameras=by_camera,
                valid_dual_rgbd_feedback=bool(base_ok), geometry_scope=obs['geometry_scope'],
                block=block, hole=hole, fixture=fixture, full_geometry=bool(base_ok and block and hole and measured_fixture),
                faults=obs['faults'], warnings=obs['quality_warnings'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for label in ('root', 'site', 'output'):
        parser.add_argument('--'+label, type=Path, required=True)
    parser.add_argument('--bundle-id', required=True)
    parser.add_argument('--seconds', type=float, default=12)
    parser.add_argument('--acquire-timeout', type=float, default=44)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 60 or not args.seconds + 3 <= args.acquire_timeout <= 150:
        parser.error('require 10–60 second capture with bounded acquisition window')
    root, site, out = (x.resolve() for x in (args.root, args.site, args.output))
    manifest = read(root/'manifest.json')
    if manifest['bundle_id'] != args.bundle_id:
        raise ValueError('bundle does not match requested release')
    sys.path[:0] = [str(root/rel) for rel in ('deps','src','control/src','upstream/RPent')]
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.utilities import get_rmw_implementation_identifier
    from sensor_msgs.msg import JointState, CompressedImage
    from f1dagger.contract import FULL_NAMES
    from f1_harness.contracts import Mode, Observation
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.ipc import UnixClient, UnixServer
    from f1_harness.owner_bridge import OwnerBridge
    from f1_harness.site import _safe_environment
    from rpent.utils.rpc.http_rpc import HttpRpcClient

    out.mkdir(parents=True, exist_ok=False)
    cfgdir=out/'site'
    cfgdir.mkdir(mode=0o700)
    runtime=Path(tempfile.mkdtemp(prefix='f1-gateway-rgbd-'))
    report=dict(bundle_id=args.bundle_id, root=str(root),site=str(site),started=time.time(), passed=False,
                required_contiguous_s=args.seconds, gateway_rpc='observe_with_status', start_sent=False,
                hardware_owner_connected=False, hardware_command_publishers=0, feedback_topic='/hal/joint_states',
                rmw=get_rmw_implementation_identifier(), middleware_environment={k:os.environ.get(k) for k in
                ('ROS_DOMAIN_ID','ROS_AUTOMATIC_DISCOVERY_RANGE','RMW_IMPLEMENTATION','CYCLONEDDS_URI')},
                files={}, samples=[], warmup=[], attempts=[], children={}, denied_ipc=[], rationale='HTTP/IPC only relocated')
    children=[]; logs=[]; ipc=engine=bridge=ros=executor=spin=None
    counters=Counter(); callbacks=Counter(); seqs={}; matches={}; match_lock=threading.Lock()
    left_times=[]; scope_id=None
    capture_fk=threading.Event(); fk_input={}; fk_worker=None
    sample_range={'name':'arm_l_j2','packets':0,'samples_below_min':0,'unique':0}
    seen_matches=set()
    report['matched_joint_feedback']=sample_range
    try:
        gateway=read(site/'gateway.local.json')
        cfg=gateway['scene']
        files=('gateway','numerics','verify','plan','masks','development','launch')
        for stem in files:
            p=site/(stem+'.local.json')
            if not p.exists() and stem=='launch':continue
            shutil.copyfile(p,cfgdir/p.name);report['files'][p.name]=dict(original=hashfile(p))
        protected=read(cfgdir/'development.local.json')['reviewed_files']
        for name in ('gateway.local.json','numerics.local.json','verify.local.json','plan.local.json','masks.local.json'):
            if protected[name] != report['files'][name]['original']:
                raise ValueError('site review hash differs: '+name)
        masks=read(cfgdir/'masks.local.json')['views']
        urls={v['sam_url'] for v in masks.values() if v.get('backend')=='sam2_remote'}
        checkpoints={v['checkpoint_sha256'] for v in masks.values()}
        if cfg.get('mask_execution')!='local_sam2' or len(urls)!=1 or len(checkpoints)!=1 or any(
                v.get('backend')!='sam2_remote' for v in masks.values()):
            raise ValueError('site must bind real SAM for all views')
        url=urls.pop();parsed=urlsplit(url)
        if parsed.hostname not in ('localhost','127.0.0.1'):raise ValueError('SAM must be loopback')
        report['sam_url']=url
        with urlopen(url.rstrip('/')+'/health', timeout=4) as response: health=json.load(response)
        report['sam_health']=health
        if health.get('ready') is not True or health.get('model_sha256') not in checkpoints:
            raise ValueError('production SAM model identity unavailable')
        sockets=[]
        try:
            for k in ('control_port','image_port'):
                s=socket.socket();s.bind(('127.0.0.1',0));sockets.append(s);gateway[k]=s.getsockname()[1]
        finally:
            for s in sockets:s.close()
        gateway['owner_socket']=str(runtime/'feedback.sock')
        cfg.update(owner_socket=gateway['owner_socket'],numerical_socket=str(runtime/'numerics.sock'),
                   verifier_socket=str(runtime/'verify.sock'))
        save(cfgdir/'gateway.local.json',gateway)
        for kind in ('numerics','verify'):
            p=cfgdir/(kind+'.local.json');edited=read(p);edited['socket']=str(runtime/(kind+'.sock'))
            if kind=='numerics':edited['spec']['gate']['runtime']=str(out/'numerical-gate-cache')
            save(p,edited)
        launch=cfgdir/'launch.local.json'
        if launch.exists():
            value=read(launch);value['runtime']=str(out/'model-cache');save(launch,value)
        for name, row in report['files'].items():row['shadow']=hashfile(cfgdir/name)
        limits=read(cfgdir/'numerics.local.json')['spec']['gate']['limits']
        low,high=limits['arm_l_j2'] if isinstance(limits,dict) else limits[1]
        sample_range.update(limits_deg=[low,high])
        def replicate_fk():
            if not capture_fk.wait(timeout=150) or not fk_input:return
            record=dict(fk_input,method='forward_pose',issued_monotonic_ns=time.monotonic_ns())
            try:
                result=UnixClient(Path(cfg['numerical_socket']),timeout=2.).call(
                    'forward_pose',full22=fk_input['full22'],held16=fk_input['measured16'])
                record['result']=result
            except Exception as exc:
                record['error_type']=type(exc).__name__;record['error']=str(exc)
            record['completed_monotonic_ns']=time.monotonic_ns()
            report['one_mirrored_fk_request']=record
        engine=TaskEngine(EvidenceStore(out/'passive-owner'), mode=Mode.READ_ONLY)
        scope_id=engine.instance
        bridge=OwnerBridge(engine,numerical_planner=None,result_verifier=None,
            feedback_age_ns=int(cfg['feedback_age_ms']*1e6),reference_tolerance_deg=cfg['reference_tolerance_deg'])
        def dispatch(method, kwargs):
            counters[method]+=1
            if method=='status':return engine.status()
            if method=='measured_feedback':
                answer=bridge.measured_feedback(**kwargs)
                if kwargs.get('target_received_ns') is not None:
                    with match_lock:
                        t=answer['matched_state_received_ns']
                        matches[kwargs['target_received_ns']]=dict(state_ns=t)
                        v=answer['full22'][1]
                        sample_range['packets']+=1
                        if t not in seen_matches:
                            seen_matches.add(t)
                            sample_range['unique']+=1
                            sample_range['samples_below_min']+=int(v<low)
                            sample_range['min_deg']=min(v,sample_range.get('min_deg',v))
                            sample_range['max_deg']=max(v,sample_range.get('max_deg',v))
                            sample_range.setdefault('first_matched_state_ns',t)
                            sample_range['last_matched_state_ns']=max(t,sample_range.get('last_matched_state_ns',t))
                        if not fk_input:
                            fk_input.update(target_received_ns=kwargs['target_received_ns'],state_received_ns=t,
                                full22=list(answer['full22']),measured16=list(answer['measured16']),
                                actual_stamps16=list(answer['stamps16']),gripper_slots='unmeasured and invalid')
                            capture_fk.set()
                return answer
            if method=='invalidate_live_scene':return engine.invalidate_live_observation(kwargs['reason'])
            if method=='ingest_live_scene':return engine.update_live_observation(Observation.model_validate(kwargs['observation']),kwargs['context'])
            if method=='ingest_observation':return engine.update_observation(Observation.model_validate(kwargs['observation']))
            report['denied_ipc'].append(method)
            raise PermissionError('readonly health preflight has no control interface')
        ipc=UnixServer(runtime/'feedback.sock',dispatch,scene_evidence=True).start()
        rclpy.init(args=None)
        ros=rclpy.create_node('f1_gateway_passive_feedback_'+str(os.getpid()),enable_rosout=False)
        executor=SingleThreadedExecutor();executor.add_node(ros)
        def joint_cb(msg):
            stamp=time.monotonic_ns();names=dict(zip(msg.name,msg.position))
            if len(msg.name)!=len(msg.position) or len(names)!=len(msg.name) or not set(FULL_NAMES)<=names.keys():
                callbacks['incomplete_joints']+=1;return
            values=[names[name] for name in FULL_NAMES]
            if not all(math.isfinite(v) for v in values):callbacks['nonfinite_joints']+=1;return
            bridge.update_feedback(full22=values,measured16=values[:14]+[0.,0.],stamps16=[stamp]*14+[None,None])
            callbacks['valid_joints']+=1
        def left_cb(msg):
            if not msg.data:return
            stamp=msg.header.stamp.sec*10**9 + msg.header.stamp.nanosec
            if seqs.get('left_source_ns')!=stamp:
                left_times.append(time.monotonic());seqs['left_source_ns']=stamp;callbacks['left_images']+=1
        left=next(c['rgb']['topic'] for c in gateway['observation']['inspection_cameras'] if c['name']=='left')
        report['left_topic']=left
        ros.create_subscription(JointState,'/hal/joint_states',joint_cb,qos_profile_sensor_data)
        ros.create_subscription(CompressedImage,left,left_cb,qos_profile_sensor_data)
        spin=threading.Thread(target=executor.spin,daemon=True);spin.start()
        fk_worker=threading.Thread(target=replicate_fk,daemon=True);fk_worker.start()

        for kind in ('verify','numerics','gateway'):
            logfile=out/(kind+'.log');handle=logfile.open('wb');logs.append(handle)
            cmd=[sys.executable,str(root/'native/entry.py'),'--run',str(out/kind),'serve',kind,str(cfgdir/(kind+'.local.json'))]
            child=subprocess.Popen(cmd,stdout=handle,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                   start_new_session=True,env=_safe_environment())
            children.append((kind,child));report['children'][kind]={'argv':cmd,'pid':child.pid}
            end=time.monotonic()+70
            while time.monotonic()<end:
                if child.poll() is not None:raise RuntimeError(kind+' exited: '+logfile.read_text(errors='replace')[-1300:])
                if '"ready": true' in logfile.read_text(errors='replace'):break
                time.sleep(.12)
            else:raise TimeoutError('native service readiness '+kind)
        rpc=HttpRpcClient('http://127.0.0.1:'+str(gateway['control_port']),enable_sessions=False)
        deadline=time.monotonic()+args.acquire_timeout;window=None;distinct=set();scene_seen=False
        longest=0.;states=Counter(); fixture_positions=[]; stream_first={}; stream_latest={}
        while time.monotonic()<deadline:
            if any(child.poll() is not None for _,child in children):raise RuntimeError('service exited during capture')
            stamp=time.monotonic();entry={'at_s':stamp}
            try:
                snapshot=rpc.call('observe_with_status',timeout_s=2.)
                status=snapshot['status']
                if status['owner_instance_id']!=scope_id or status['authorized'] or status['running'] or status['pending']:
                    raise RuntimeError('unexpected owner/authorization')
                obs=snapshot['observation']
                with match_lock:entry.update(scene_metrics(obs,matches,cfg))
                if entry['valid_dual_rgbd_feedback']:
                    if window is None:
                        window=stamp;distinct=set();scene_seen=False;stream_first={};stream_latest={}
                    for camera,details in entry['cameras'].items():
                        for frame in details['frames']:
                            name=camera+'.'+frame['stream']
                            value=frame['received_ns']
                            stream_first.setdefault(name,value);stream_latest[name]=value
                    distinct.add(obs['observation_id']);scene_seen |= entry['full_geometry']
                    if entry['full_geometry'] and len(fixture_positions)<3:
                        fixture_positions.append(dict(observation_id=obs['observation_id'],block=entry['block'],hole=entry['hole'],fixture=entry['fixture']))
                else:window=None
            except Exception as exc:
                window=None;entry['error']=str(exc)[:700]
            states[entry.get('error') or ('paired' if entry['valid_dual_rgbd_feedback'] else 'partial')]+=1
            report['samples'].append(entry)
            if window is not None:
                longest=max(longest,stamp-window)
                left_window=[t for t in left_times if t>=window]
                # Evaluate actual updates over the whole interval, independent of process startup.
                left_span=left_window[-1]-left_window[0] if len(left_window)>1 else 0.
                interval_frames_s={name:(stream_latest[name]-first)/1e9 for name,first in stream_first.items()}
                if (stamp-window>=args.seconds and len(distinct)>1 and scene_seen and left_span>=10
                        and len(interval_frames_s)==4 and min(interval_frames_s.values())>=10):
                    report.update(passed=True,contiguous_s=stamp-window,interval_left_images=len(left_window),
                                  interval_left_span_s=left_span,unique_paired_scenes=len(distinct),rgbd_frame_advancement_s=interval_frames_s)
                    break
            time.sleep(.18)
        report.update(longest_paired_window_s=longest, request_outcomes=dict(states),geometry_examples=fixture_positions)
        with urlopen(url.rstrip('/')+'/health', timeout=4) as response:report['sam_end_health']=json.load(response)
        try:
            inspection=rpc.call('camera_observation',timeout_s=3.)
            report['gateway_inspection_rgb']=[dict(camera=f['camera'],sequence=f['sequence'],source_ns=f['source_ns']) for f in inspection['frames']]
        except Exception as exc:report['gateway_inspection_error']=str(exc)
        report['feedback_ros_publishers']=[p.topic_name for p in ros.publishers]
        report['topic_publishers']={t:ros.count_publishers(t) for t in set(
            [left,'/hal/joint_states']+[v[k]['topic'] for v in gateway['observation']['cameras'] for k in ('rgb','depth')])}
        if any(p.startswith(('/motion_ctl/','/move/')) for p in report['feedback_ros_publishers']):raise ValueError('unexpected ROS publisher')
        if report['denied_ipc'] or engine.status()['authorized'] or engine.request_publications:raise ValueError('unapproved control request')
    except Exception as exc:
        report['passed']=False;report['error']=type(exc).__name__+': '+str(exc)
    finally:
        capture_fk.set()
        if fk_worker:fk_worker.join(timeout=3)
        for kind,child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGINT)
                try:child.wait(timeout=9)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
            report['children'][kind]['exit_code']=child.returncode
        for log in logs:log.close()
        if ipc:ipc.close()
        if executor:executor.shutdown(timeout_sec=2.)
        if spin:spin.join(timeout=2)
        if ros:ros.destroy_node()
        if rclpy.ok():rclpy.shutdown()
        if bridge:bridge.executor.shutdown(wait=False,cancel_futures=True)
        if engine:report.update(final_authorized=engine.authorized,request_publications=len(engine.request_publications))
        report['callback_counts']=dict(callbacks);report['owner_ipc_methods']=dict(counters);report['finished']=time.time()
        save(out/'result.json',report)
        shutil.rmtree(runtime)
    print(json.dumps({k:v for k,v in report.items() if k in ('passed','error','contiguous_s','callback_counts','request_outcomes','longest_paired_window_s','topic_publishers','unique_paired_scenes')},ensure_ascii=False))
    return 0 if report['passed'] else 2


if __name__=='__main__':
    raise SystemExit(main())
