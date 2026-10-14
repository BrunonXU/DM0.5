"""Standalone ROS subscriber + Unix command socket. Starts idle, never moves robot."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socketserver
import threading
import time
import uuid

from core import atomic_json, utc
from project_paths import read_profile

TASK = 'F1 upper-body bimanual teleoperation'
CAMERAS = ('head', 'left', 'right')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--check', action='store_true', help='Import/dependency check only; no ROS node or recording')
    args = parser.parse_args()
    runtime = Path(args.runtime).resolve()
    if args.check:
        import importlib
        import shutil
        from recording.upstream.lerobot_v3 import FEATURE_NAMES
        modules = {}
        for name in ('numpy','pyarrow','rclpy','sensor_msgs.msg','control_msgs.msg'):
            module = importlib.import_module(name)
            modules[name] = getattr(module,'__version__','installed')
        for name in ('ffmpeg','ffprobe'):
            if not shutil.which(name): raise ValueError(name+' is missing')
        print(json.dumps({'ok':True,'contract':'f1_upper_16d_v1','feature_names':FEATURE_NAMES,'dependencies':modules}))
        return
    machine_profile = read_profile()
    if Path('/etc/machine-id').read_text().strip() != machine_profile['expected_machine_id']:
        raise ValueError('recording worker machine identity differs from selected profile')
    owned = runtime / 'recorder'
    owned.mkdir(parents=True, exist_ok=True)
    lock = (owned/'worker.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.umask(0o077)
    import numpy as np
    import pyarrow
    import pyarrow.parquet as pq
    import rclpy
    from recording.upstream.lerobot_recorder import LeRobotRecorderNode
    from recording.upstream.lerobot_v3 import CAMERA_KEYS
    config_path = runtime/'recording.json'
    config = json.loads((config_path if config_path.is_file() else Path(__file__).parent/'config.json').read_text())
    config['recording_session_id'] = uuid.uuid4().hex
    config['machine_id'] = machine_profile['expected_machine_id']
    config['profile_sha256'] = hashlib.sha256(json.dumps(machine_profile,sort_keys=True).encode()).hexdigest()
    output = runtime/'recordings'/'upper16_v1'
    if config.get('recording_format') == 'f1_capture_v2':
        from recording.unified.storage import read_config
        output = Path(read_config(runtime/'storage.json')['paths']['pico'])
    elif output.is_symlink() or not output.resolve().is_relative_to(runtime):
        raise ValueError('recording root must remain inside this runtime')
    status_file = owned/'status.json'
    socket_path = owned/'control.sock'
    closed = threading.Event()
    last_error = [None]
    last_saved = [None]

    class Recorder(LeRobotRecorderNode):
        def _reject_active_episode(self, reason):
            capture = self._capture
            previous = capture.path if capture else None
            last_error[0] = str(reason)
            super()._reject_active_episode(reason)
            if previous:
                recovery = output/'recovery'/previous.name
                last_saved[0] = {'kind':'recovery','relative_path':str(recovery.relative_to(output)),'reason':str(reason)}

    rclpy.init()
    node = Recorder(output, config)

    def catalog():
        if config.get('recording_format') == 'f1_capture_v2':
            return []  # The common raw catalogue reads committed captures directly.
        info_path = output/'meta/info.json'
        if not info_path.exists():
            atomic_json(owned/'catalog.json', {'episodes':[]}); return []
        info = json.loads(info_path.read_text())
        rows = []
        preview_root = owned/'previews'; preview_root.mkdir(parents=True,exist_ok=True)
        for path in sorted((output/'meta/episodes').glob('chunk-*/*.parquet')):
            for row in pq.read_table(path).to_pylist():
                index = int(row['episode_index'])
                if index >= int(info['total_episodes']): continue
                chunk, file_index = int(row['data/chunk_index']), int(row['data/file_index'])
                rel = f'data/chunk-{chunk:03d}/file-{file_index:03d}.parquet'
                source_root = output/'source'/f'episode-{index:06d}'
                arrays_file = source_root/'episode_arrays.npz'
                started_at = None
                analytics = None
                if arrays_file.exists():
                    with np.load(arrays_file) as arrays:
                        stamps = arrays['timestamp_ns']; started_at = time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime(int(stamps[0])/1e9))+'+00:00'
                        indices = np.unique(np.linspace(0,len(stamps)-1,min(2000,len(stamps)),dtype=int))
                        names = info['features']['action']['names']
                        analytics = {'timestamps_s':(indices/float(info['fps'])).tolist(),
                                     'source_timestamps_s':((stamps[indices]-stamps[0])/1e9).tolist(),
                                     'state':arrays['state'][indices].tolist(),'action':arrays['action'][indices].tolist(),
                                     'action_held':arrays['action_held'][indices].tolist(),
                                     'action_source_age_s':(arrays['action_source_age_ns'][indices]/1e9).tolist(),
                                     'channel_names':names,'units':['deg']*14+['percent']*2,
                                     'duration_s':len(stamps)/float(info['fps']),'sample_count':len(indices),
                                     'source_frame_count':len(stamps),'downsampled':len(indices)<len(stamps),
                                     'notes':['播放器时间=帧索引/fps；真实源时间间隔单独保留，不把二者当同一时钟','action为真实命令；夹爪hold有writer/epoch/原始age标记，训练验收未完成']}
                paths = [output/rel, output/'meta/episodes'/f'chunk-{chunk:03d}'/f'file-{file_index:03d}.parquet']
                videos = []
                eid = hashlib.sha256(('lerobot:'+str(output)+':'+str(index)).encode()).hexdigest()[:24]
                if analytics: atomic_json(preview_root/(eid+'.json'),analytics)
                for camera, key in zip(CAMERAS, CAMERA_KEYS):
                    video = output/'videos'/key/f'chunk-{chunk:03d}'/f'file-{file_index:03d}.mp4'
                    paths.append(video)
                    videos.append({'camera':camera,'url':f'/api/data/video/{eid}/{camera}.mp4','relative_path':str(video.relative_to(output))})
                files = [{'name':p.name,'relative_path':str(p.relative_to(output)),'size_bytes':p.stat().st_size,'modified_at':p.stat().st_mtime} for p in paths]
                retained_paths = [p for base in (source_root, output/'alignment'/f'episode-{index:06d}') if base.exists() for p in base.rglob('*') if p.is_file() and not p.is_symlink()]
                disk_bytes = sum(p['size_bytes'] for p in files) + sum(p.stat().st_size for p in retained_paths)
                rows.append({'id':eid,'name':f'LeRobot episode_{index:06d}','episode_index':index,'relative_path':rel,
                             'source':'lerobot','format':'lerobot_v3_upper16','contract':'f1_upper_16d_v1','source_basis':info.get('source_identity_basis'),'files':files,
                             'videos':videos,'size_bytes':sum(p['size_bytes'] for p in files),'total_disk_bytes':disk_bytes,'size_scope':'committed_training_bundle; total_disk_bytes includes retained source and alignment','file_count':len(files),
                             'duration_s':int(row['length'])/float(info['fps']),'message_count':int(row['length']),
                             'started_at':started_at,'modified_at':max(p['modified_at'] for p in files),
                             'metadata_status':'parsed','metadata_sha256':hashlib.sha256(paths[1].read_bytes()).hexdigest(),
                             'training_accepted':False,'preview_url':None,'analytics_url':f'/api/data/analytics/{eid}' if analytics else None,'topics':[]})
        atomic_json(owned/'catalog.json', {'episodes':rows,'output_root':str(output),'updated_at':utc()})
        return rows

    def state():
        mode = node._mode
        reasons = []
        ready = False
        if mode == 'idle':
            try:
                _, reason, _ = node._ready_snapshot()
                ready = reason is None
                if reason: reasons.append(reason)
            except Exception as exc: reasons.append(str(exc))
        capture = node._capture
        review = None
        review_arrays = node._review_arrays
        review_timing = node._review_timing_info
        if mode == 'review' and capture and review_arrays is not None:
            info = review_timing or {}
            review = {'frames':len(review_arrays['timestamp_ns']),
                      'duration_s':len(review_arrays['timestamp_ns'])/float(config['fps']),
                      'training_ready':bool(info.get('training_ready')),'coverage_fault':info.get('coverage_fault'),
                      'relative_path':str(capture.path.relative_to(output)),
                      'videos':[] if config.get('recording_format') == 'f1_capture_v2' else [{'camera':camera,'url':f'/api/recording/review/{camera}.mp4'} for camera in CAMERAS],
                      'raw_save_allowed':bool(info.get('raw_save_allowed'))}
        return {'mode':mode,'available':True,'ready':ready,'ready_reasons':reasons,'output_root':str(output),
                'contract':'f1_upper_16d_v1','source_basis':'verified_pico_session_and_graph','pico_session_identity':node._pico_source.signature,'task':TASK,'fps':config['fps'],'frames':capture.frame_count if capture else 0,
                'elapsed_s':max(0,time.monotonic()-capture.started_monotonic) if capture and mode=='recording' else 0,
                'training_accepted':False,'last_error':last_error[0],'last_saved':last_saved[0],'review':review,
                'pid':os.getpid(),'session_id':config['recording_session_id'],'checked_unix':time.time()}

    def heartbeat():
        last_scan = 0.0
        while not closed.wait(.4):
            try:
                atomic_json(status_file, state())
                if config.get('recording_format') == 'f1_capture_v2' and node._mode == 'idle' and time.monotonic()-last_scan>10:
                    from recording.unified.jobs import scan
                    scan(runtime); last_scan=time.monotonic()
            except Exception as exc: print('RECORDER_STATUS_RETRY '+str(exc),flush=True)

    def command(operation):
        last_error[0] = None
        if operation == 'start':
            if node._mode != 'idle': raise ValueError('录制器不是空闲状态')
            _, fault, _ = node._ready_snapshot()
            if fault: raise ValueError('开录条件未满足: '+fault)
            node.start_recording()
            if node._mode != 'recording': raise ValueError('录制启动失败，请检查反馈与相机')
        elif operation == 'stop':
            if node._mode != 'recording': raise ValueError('当前没有正在录制的片段')
            node.stop_recording()
        elif operation == 'save':
            if node._mode != 'review': raise ValueError('当前没有待确认的片段')
            capture_path = node._capture.path
            node.save_review()
            if config.get('recording_format') == 'f1_capture_v2' and node._mode == 'idle' and not last_error[0]:
                saved = output/capture_path.name.removesuffix('.partial')
                last_saved[0] = dict(kind='committed',path=str(saved),training_ready=False)
            if node._mode == 'review': raise ValueError('保存未完成，原片段仍保留，可重试；检查 recorder 日志')
            rows = catalog()
            if not last_error[0] and rows:
                row = rows[-1]
                last_saved[0] = {'kind':'committed','id':row['id'],'episode_index':row['episode_index'],'relative_path':row['relative_path']}
        elif operation == 'discard':
            if node._mode != 'review': raise ValueError('当前没有待确认的片段')
            node.discard_review()
        else: raise ValueError('未知录制命令')
        current = state(); atomic_json(status_file, current)
        return current

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            try:
                raw = self.rfile.readline(4097)
                if len(raw)>4096: raise ValueError('请求过大')
                request = json.loads(raw)
                result = {'ok':True,'recording':command(request.get('operation'))}
            except Exception as exc:
                last_error[0] = str(exc); result = {'ok':False,'error':str(exc),'recording':state()}
            try: self.wfile.write(json.dumps(result,ensure_ascii=False).encode()+b'\n')
            except (BrokenPipeError,ConnectionResetError): pass

    if socket_path.exists(): socket_path.unlink()
    server = socketserver.UnixStreamServer(str(socket_path), Handler)
    os.chmod(socket_path,0o600)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    threading.Thread(target=heartbeat,daemon=True).start()
    catalog(); atomic_json(status_file,state())
    def stop(*_): closed.set()
    signal.signal(signal.SIGINT,stop); signal.signal(signal.SIGTERM,stop)
    try:
        while not closed.is_set() and rclpy.ok(): rclpy.spin_once(node,timeout_sec=.1)
    finally:
        closed.set();server.shutdown();server.server_close();socket_path.unlink(missing_ok=True)
        node.destroy_node()
        final=state();final.update(mode='not_started',ready=False);atomic_json(status_file,final)
        rclpy.shutdown()

if __name__ == '__main__': main()
