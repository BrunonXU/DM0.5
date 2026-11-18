"""Bounded read-only ROS evidence for reusing a coarse calibration after reboot.

Only subscriptions and the two camera device-information clients are created.
The original calibration is never changed; a separate verifier issues the receipt.
"""
import hashlib
import json
import math
from pathlib import Path
import time
import uuid


BODY_HEAD_NAMES = tuple('head_j' + str(i) for i in range(1, 4)) + tuple(
    'body_j' + str(i) for i in range(1, 6))
DEVICE_SERVICES = {
    'head': '/camera/head/get_device_info',
    'right': '/camera/right_wrist/device_info',
}


def _boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _json_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('boot_capture_regular_file_required:' + path.name)
    data = path.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def canonical_camera_info(message):
    """Match the existing calibration/observer's CameraInfo content identity."""
    return dict(width=message.width, height=message.height,
        frame_id=message.header.frame_id, distortion_model=message.distortion_model,
        k=list(message.k), d=list(message.d), r=list(message.r), p=list(message.p))


class BootSamples:
    def __init__(self):
        self.info = {}
        self.info_received_ns = {}
        self.info_changes = set()
        self.joints = []

    def camera(self, camera, stream, message, received_ns):
        key = camera, stream
        value = canonical_camera_info(message)
        if key in self.info and self.info[key] != value:
            self.info_changes.add(camera + ':' + stream)
        self.info[key] = value
        self.info_received_ns[key] = received_ns

    def joint(self, message, received_ns):
        names, values = list(message.name), list(message.position)
        if len(names) != len(values) or len(names) != len(set(names)):
            raise ValueError('boot_capture_joint_mapping_invalid')
        by_name = dict(zip(names, values))
        if any(name not in by_name or not math.isfinite(by_name[name]) for name in BODY_HEAD_NAMES):
            raise ValueError('boot_capture_body_head_feedback_missing')
        # Retain at most 100 Hz of feedback; this bounds the raw JSON evidence
        # independently of a driver's publication rate while preserving a window.
        if self.joints and received_ns - self.joints[-1]['received_ns'] < 10_000_000:
            return
        if len(self.joints) >= 4096:
            raise ValueError('boot_capture_joint_budget_exceeded')
        self.joints.append(dict(received_ns=received_ns,
            source_ns=message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec,
            body_head_deg={name: float(by_name[name]) for name in BODY_HEAD_NAMES}))

    def complete(self, now_ns):
        return (set(self.info) == {(camera, stream) for camera in ('head', 'right')
                                  for stream in ('rgb', 'depth')}
                and not self.info_changes and len(self.joints) >= 2
                and self.joints[-1]['received_ns'] - self.joints[0]['received_ns'] >= 300_000_000
                and 0 <= now_ns - self.joints[-1]['received_ns'] <= 500_000_000
                and all(0 <= now_ns - value <= 500_000_000 for value in self.info_received_ns.values()))

    def fields(self):
        return dict(camera_info={camera: {stream: self.info.get((camera, stream))
                    for stream in ('rgb', 'depth')} for camera in ('head', 'right')},
            camera_info_received_ns={camera: {stream: self.info_received_ns.get((camera, stream))
                    for stream in ('rgb', 'depth')} for camera in ('head', 'right')},
            camera_info_changes=sorted(self.info_changes), joint_samples=self.joints,
            body_head_deg=self.joints[-1]['body_head_deg'] if self.joints else {})


class NativeBootSession:
    """Reusable read-only discovery resources; every collect starts new evidence.

    The caller owns serialization and can spin_idle between requests. No capture
    samples or device replies survive a request. This class creates no publisher.
    """
    def __init__(self, profile):
        import threading
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CameraInfo, JointState
        self.rclpy=rclpy;self.context=Context();self.node=None;self.executor=None
        self.clients={};self.service_types={};self.samples=None;self.closed=False
        self.lock=threading.Lock();self.instance_id=uuid.uuid4().hex
        self.profile=json.loads(json.dumps(profile));self.subscriptions=[]
        cameras={row['name']:row for row in self.profile['cameras']}
        if set(cameras)!={'head','right'} or len(self.profile['cameras'])!=2:
            raise ValueError('boot_capture_head_and_right_required')
        try:
            rclpy.init(args=[],context=self.context)
            self.node=rclpy.create_node('f1_harness_boot_readonly_'+uuid.uuid4().hex[:8],
                context=self.context,enable_rosout=False,start_parameter_services=False)
            self.executor=SingleThreadedExecutor(context=self.context);self.executor.add_node(self.node)
            for camera,config in cameras.items():
                for stream in ('rgb','depth'):
                    self.subscriptions.append(self.node.create_subscription(CameraInfo,
                        config[stream]['camera_info_topic'],
                        lambda msg,c=camera,s=stream:self._camera(c,s,msg),qos_profile_sensor_data))
            self.subscriptions.append(self.node.create_subscription(JointState,'/hal/joint_states',
                self._joint,qos_profile_sensor_data))
        except BaseException:
            self.close();raise

    def _camera(self,camera,stream,message):
        if self.samples is not None:self.samples.camera(camera,stream,message,time.monotonic_ns())

    def _joint(self,message):
        if self.samples is not None:self.samples.joint(message,time.monotonic_ns())

    def spin_idle(self,seconds=.05):
        if not 0<=seconds<=.1:raise ValueError('boot_idle_spin_bound')
        if not self.lock.acquire(blocking=False):raise RuntimeError('boot_capture_busy')
        try:
            if self.closed:raise RuntimeError('boot_capture_closed')
            self.executor.spin_once(timeout_sec=seconds)
        finally:self.lock.release()

    def collect(self,*,seconds=8.):
        _capture_duration(seconds)
        if not self.lock.acquire(blocking=False):raise RuntimeError('boot_capture_busy')
        try:
            if self.closed:raise RuntimeError('boot_capture_closed')
            return self._collect(seconds)
        finally:self.samples=None;self.lock.release()

    def _collect(self,seconds):
        from rosidl_runtime_py.utilities import get_service
        from rosidl_runtime_py.convert import message_to_ordereddict
        # Start from a new window, never cached samples or serial-number replies.
        self.samples=samples=BootSamples();futures={};device_results={};errors=[]
        start_ns=time.monotonic_ns();deadline_ns=start_ns+int(seconds*1e9)
        try:
            while time.monotonic_ns()<deadline_ns:
                services=dict(self.node.get_service_names_and_types())
                for camera,name in DEVICE_SERVICES.items():
                    if name in services:
                        types=services[name]
                        if len(types)!=1:raise ValueError('boot_capture_ambiguous_device_service:'+camera)
                        if camera in self.service_types and self.service_types[camera][1]!=types[0]:
                            raise ValueError('boot_capture_device_service_type_changed:'+camera)
                        if camera not in self.clients:
                            typ=get_service(types[0]);self.clients[camera]=self.node.create_client(typ,name)
                            self.service_types[camera]=(typ,types[0])
                    if camera in self.clients and camera not in futures and self.clients[camera].service_is_ready():
                        typ,_=self.service_types[camera]
                        futures[camera]=self.clients[camera].call_async(typ.Request())
                    future=futures.get(camera)
                    if future is not None and future.done() and camera not in device_results:
                        value=message_to_ordereddict(future.result())
                        if value.get('success') is False:raise ValueError('boot_capture_device_query_failed:'+camera)
                        serial=value.get('info',value).get('serial_number')
                        if not isinstance(serial,str) or not serial:raise ValueError('boot_capture_device_serial_missing:'+camera)
                        device_results[camera]=dict(service=name,type=self.service_types[camera][1],
                            result=value,serial_number=serial,received_ns=time.monotonic_ns())
                now=time.monotonic_ns()
                if samples.info_changes:raise ValueError('boot_capture_camera_info_changed:'+','.join(sorted(samples.info_changes)))
                if set(device_results)=={'head','right'} and samples.complete(now):break
                remaining=(deadline_ns-now)/1e9
                if remaining>0:self.executor.spin_once(timeout_sec=min(.05,remaining))
            completed_ns=time.monotonic_ns()
            complete=set(device_results)=={'head','right'} and samples.complete(completed_ns)
            if not complete:errors.append('boot_capture_missing_or_stale_measurements')
        except Exception as exc:
            complete=False;completed_ns=time.monotonic_ns();errors.append(type(exc).__name__+':'+str(exc))
        finally:
            for camera,future in futures.items():
                if not future.done():
                    future.cancel()
                    remove=getattr(self.clients[camera],'remove_pending_request',None)
                    if remove is not None:remove(future)
        return dict(samples.fields(),device_info=device_results,
            camera_serials={camera:row['serial_number'] for camera,row in device_results.items()},
            window_start_received_ns=start_ns,observed_monotonic_ns=completed_ns,
            requested_seconds=seconds,complete_window=complete,errors=errors,
            capture_instance_id=self.instance_id,capture_request_id=uuid.uuid4().hex,
            command_publishers=0,motion_authorized=False)

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True;self.samples=None
            if self.executor is not None:self.executor.shutdown(timeout_sec=.5)
            if self.node is not None:
                for client in self.clients.values():self.node.destroy_client(client)
                self.node.destroy_node()
            if self.context.ok():self.rclpy.shutdown(context=self.context)


def _capture_duration(seconds):
    if not isinstance(seconds,(float,int)) or isinstance(seconds,bool) or not 1<=seconds<=8:
        raise ValueError('boot_capture_duration_must_be_1_to_8_seconds')


def _collect_native(profile, *, seconds=8.):
    _capture_duration(seconds)
    session=None;start=time.monotonic_ns()
    try:
        session=NativeBootSession(profile)
        return session.collect(seconds=seconds)
    except Exception as exc:
        return dict(BootSamples().fields(),device_info={},camera_serials={},
            window_start_received_ns=start,observed_monotonic_ns=time.monotonic_ns(),
            requested_seconds=seconds,complete_window=False,errors=[type(exc).__name__+':'+str(exc)])
    finally:
        if session is not None:session.close()


def capture_boot_recheck(site, output, *, session=None):
    """Write raw evidence and, only after validation, a new boot-recheck.json."""
    from .boot_recheck import build_boot_receipt
    site, output = Path(site).resolve(), Path(output).resolve()
    record, _ = _json_file(site / 'development.local.json')
    calibration_path = Path(record['calibration']['path'])
    if not calibration_path.is_absolute():
        calibration_path = site / calibration_path
    calibration, calibration_sha = _json_file(calibration_path)
    if calibration_sha != record['calibration']['sha256']:
        raise ValueError('boot_capture_calibration_identity_changed')
    gateway, _ = _json_file(site / 'gateway.local.json')
    if session is not None and (not isinstance(session,NativeBootSession)
            or session.profile!=gateway['observation']):
        raise ValueError('boot_capture_session_profile_mismatch')
    output.mkdir(parents=True, exist_ok=True)
    evidence_path, receipt_path = output / 'boot-recheck-evidence.json', output / 'boot-recheck.json'
    if any(path.exists() or path.is_symlink() for path in (evidence_path, receipt_path)):
        raise FileExistsError('boot_recheck_evidence_must_be_new')
    current_boot = _boot_id()
    raw = dict(schema='f1-harness-boot-capture-v1', current_boot_id=current_boot,
        capture_boot_id=calibration['boot_id'], calibration_sha256=calibration_sha,
        calibration_data_id=calibration['calibration_data_id'],
        machine_id=Path('/etc/machine-id').read_text().strip(), synthetic=False,
        command_publishers=0, motion_authorized=False, precision_certified=False)
    raw['model'] = {}
    for label in ('urdf', 'mapping'):
        path = Path(record['model'][label + '_path'])
        if not path.is_absolute():
            path = site / path
        if path.is_symlink() or not path.is_file():
            raise ValueError('boot_capture_regular_model_file_required:' + label)
        raw['model'][label + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    raw.update(_collect_native(gateway['observation']) if session is None else session.collect())
    raw['finished_boot_id'] = _boot_id()
    with evidence_path.open('x') as stream:
        json.dump(raw, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    if raw['finished_boot_id'] != current_boot:
        raise ValueError('boot_changed_during_recheck')
    if not raw['complete_window']:
        raise ValueError('boot_recheck_capture_failed:' + ';'.join(raw['errors']))
    receipt = build_boot_receipt(raw, evidence_path, calibration=calibration,
        calibration_sha256=calibration_sha, scene=gateway['scene'],
        task_hash=record['task_hash'], current_boot_id=current_boot, now_ns=time.monotonic_ns())
    with receipt_path.open('x') as stream:
        json.dump(receipt, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    return receipt
