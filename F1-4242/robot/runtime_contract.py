"""Read-only startup verification, never publish control or change modes.

ROS parameters are the node's configured values, not an SDK mode/enable ACK.
This snapshot cannot prove later parameter or physical controller state changes.
"""
from datetime import datetime, timezone
import hashlib
import math
import os
import json
import subprocess
import sys
from pathlib import Path
import time
import uuid

AUDITED_BINARY_SHA256 = {
    '898ef58fb7118ecbf416af134e01700ed38ffdd3480293cf06fe95da14bce0c8',
    '1ec598c860672824d46fe93f8f7c784002ed7612f666d6660c1f925206086ddc',
}


def verify_process(expected_sha256, *, proc_root=Path('/proc'), uid=None):
    """Hash the unique same-user running executable, with PID/exe race checks."""
    if expected_sha256 not in AUDITED_BINARY_SHA256:
        raise ValueError('控制器SHA256没有对应的已审计限位/夹爪合同')
    uid = os.getuid() if uid is None else uid
    candidates = []
    for entry in Path(proc_root).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid and (entry/'comm').read_text().strip() == 'motor_ctl_node':
                candidates.append(entry)
        except (FileNotFoundError, PermissionError):
            continue
    if len(candidates) != 1:
        raise RuntimeError(f'需要同用户唯一motor_ctl_node，发现{len(candidates)}个')
    process = candidates[0]
    def start_ticks():
        return (process/'stat').read_text().rsplit(') ', 1)[1].split()[19]
    started = start_ticks()
    command = (process/'cmdline').read_bytes()
    arguments = command.split(b'\0')
    node_names = [x for x in arguments if x.startswith((b'__node:=', b'__name:='))]
    namespaces = [x for x in arguments if x.startswith(b'__ns:=')]
    if node_names not in ([b'__node:=motor_ctl'], [b'__name:=motor_ctl']) or namespaces not in ([], [b'__ns:=/']):
        raise RuntimeError('运行进程不能唯一绑定/motor_ctl参数服务')
    exe = process/'exe'
    resolved = exe.resolve(strict=True)
    if resolved.name != 'motor_ctl_node':
        raise RuntimeError('进程实际exe名称不匹配')
    before = exe.stat()
    digest = hashlib.sha256()
    with exe.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    after = exe.stat()
    if started != start_ticks() or command != (process/'cmdline').read_bytes() or (before.st_dev,before.st_ino) != (after.st_dev,after.st_ino):
        raise RuntimeError('读取期间控制器进程或二进制发生变化')
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(f'运行控制器SHA256不匹配：{actual}')
    return {'pid': int(process.name), 'uid': uid, 'process_start_ticks': started,
            'exe': str(resolved), 'sha256': actual,
            'evidence': 'same_user_unique_running_proc_exe_not_installed_file_only'}


def read_motor_parameters(expected, timeout_s=6.0):
    """List then get exactly the requested declared parameters via current RMW."""
    if not isinstance(expected, dict) or not expected:
        raise ValueError('缺少expected_motor_parameters')
    if not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
        raise ValueError('参数读取超时需要在0..30秒内')
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.signals import SignalHandlerOptions
    from rcl_interfaces.srv import GetParameters, ListParameters
    context, node, executor = Context(), None, None
    started = time.monotonic()
    try:
        rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = rclpy.create_node('f1_console_contract_'+uuid.uuid4().hex,
            context=context, use_global_arguments=False, enable_rosout=False, start_parameter_services=False)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        deadline = started+timeout_s
        def call(kind, name, request):
            client = node.create_client(kind, name)
            future = None
            try:
                while not client.service_is_ready():
                    if time.monotonic() >= deadline:
                        raise RuntimeError(name+'发现超时')
                    executor.spin_once(timeout_sec=0.03)
                future = client.call_async(request)
                while not future.done():
                    if time.monotonic() >= deadline:
                        raise RuntimeError(name+'响应超时')
                    executor.spin_once(timeout_sec=0.03)
                response = future.result()
                if response is None:
                    raise RuntimeError(name+'没有响应')
                return response
            finally:
                if future is not None and not future.done():
                    future.cancel()
                node.destroy_client(client)
        listed = call(ListParameters, '/motor_ctl/list_parameters', ListParameters.Request())
        declared = list(listed.result.names)
        missing = [name for name in expected if name not in declared]
        if missing:
            raise RuntimeError('控制器未声明必需参数：'+', '.join(missing))
        request = GetParameters.Request()
        request.names = list(expected)
        response = call(GetParameters, '/motor_ctl/get_parameters', request)
        if len(response.values) != len(expected):
            raise RuntimeError(f'参数返回数量异常：{len(response.values)}/{len(expected)}')
        fields = {1: 'bool_value', 2: 'integer_value', 3: 'double_value', 4: 'string_value'}
        values = {}
        for name, value in zip(request.names, response.values):
            if value.type not in fields:
                raise RuntimeError('参数类型未支持/未定义：'+name)
            actual = getattr(value, fields[value.type])
            target = expected[name]
            if isinstance(actual,float) and not math.isfinite(actual):
                raise RuntimeError('非有限参数：'+name)
            # bool must not equal numeric 0/1 by Python coercion.
            if type(actual) is not type(target) or actual != target:
                raise RuntimeError(f'本机参数变化：{name}={actual!r}，profile={target!r}')
            values[name] = actual
        return {'service': '/motor_ctl/get_parameters', 'declared_names': declared,
                'requested_names': request.names, 'values': values, 'elapsed_s':time.monotonic()-started,
                'rmw': os.environ.get('RMW_IMPLEMENTATION'),
                'scope': 'declared_ros_parameters_not_sdk_current_mode_or_execution_ack'}
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=1)
        if node is not None:
            node.destroy_node()
        if context.ok():
            context.shutdown()


def read_motor_parameters_isolated(expected, timeout_s, rmw):
    if rmw not in ('rmw_fastrtps_cpp','rmw_cyclonedds_cpp'):
        raise ValueError('不支持的只读参数通信配置')
    if not isinstance(timeout_s,(int,float)) or not math.isfinite(timeout_s) or not 0<timeout_s<=30:
        raise ValueError('参数读取超时需要在0..30秒内')
    environment=dict(os.environ,RMW_IMPLEMENTATION=rmw,PYTHONDONTWRITEBYTECODE='1')
    if rmw=='rmw_fastrtps_cpp':environment.pop('CYCLONEDDS_URI',None)
    child=subprocess.run([sys.executable,'-m','robot.parameter_probe'],
        input=json.dumps({'expected':expected,'timeout_s':timeout_s}),text=True,capture_output=True,
        env=environment,cwd=Path(__file__).resolve().parents[1],timeout=timeout_s+3,check=False)
    try:payload=json.loads(child.stdout)
    except (ValueError,TypeError) as exc:raise RuntimeError('只读参数子进程没有返回有效结果') from exc
    if child.returncode or not isinstance(payload,dict) or payload.get('ok') is not True:
        raise RuntimeError('只读参数核验失败: '+str(payload.get('error','未知错误') if isinstance(payload,dict) else '响应格式无效'))
    result=payload.get('result')
    if not isinstance(result,dict) or result.get('values')!=expected or result.get('rmw')!=rmw:
        raise RuntimeError('只读参数子进程合同不匹配')
    return {**result,'transport':'isolated_read_only_process'}


def verify_runtime_contract(profile, timeout_s=6.0):
    """Return a failed report on missing/changed identity, executable or params.

    The caller must retain read-only mode if ``ok`` is false. This module never
    mutates ``profile`` and never treats known-other-robot defaults as evidence.
    """
    result = {'ok': False, 'errors': [], 'observed_utc':datetime.now(timezone.utc).isoformat(),
              'observed_monotonic_s': time.monotonic(), 'control_publishers_created':False,
              'mode_changes':False, 'scope':'startup_contract_only_not_motion_acceptance'}
    try:
        expected_id = profile.get('expected_machine_id')
        if not isinstance(expected_id, str) or len(expected_id) != 32:
            raise ValueError('缺少明确的expected_machine_id')
        actual_id = Path('/etc/machine-id').read_text().strip()
        result['machine_id'] = actual_id
        if actual_id != expected_id:
            raise RuntimeError('machine-id与F1本机profile不匹配')
        from .joint_contract import verify_joint_contract
        result['joint_configuration'] = verify_joint_contract(profile)
        joint_configuration_required = (profile.get('id') == 'F1-09' or
                                        profile.get('robot_id') == 'F1-09' or
                                        result['joint_configuration']['configured'])
        if joint_configuration_required and not result['joint_configuration']['ok']:
            errors = result['joint_configuration'].get('errors') or ['缺少本机关节配置基线']
            raise RuntimeError('关节配置不一致：' + '; '.join(errors))
        result['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        result['controller'] = verify_process(profile.get('controller_binary_sha256'))
        expected = profile.get('expected_motor_parameters')
        required = {'arm_control_mode', 'arm_impedance_type', 'left_gripper_type',
                    'right_gripper_type', 'enable_udp_motors', 'udp_motor_host', 'udp_motor_send_port'}
        if not isinstance(expected, dict) or not required.issubset(expected):
            raise ValueError('expected_motor_parameters缺少手臂/夹爪/UDP合同字段')
        parameter_rmw=profile.get('controller_parameter_rmw')
        result['parameters'] = (read_motor_parameters_isolated(expected,timeout_s,parameter_rmw)
                                if parameter_rmw else read_motor_parameters(expected, timeout_s))
        # Bind the returned parameters to the same still-running executable.
        after=verify_process(profile.get('controller_binary_sha256'))
        if any(after.get(k)!=result['controller'].get(k) for k in ('pid','process_start_ticks','sha256')):
            raise RuntimeError('读取参数期间电机进程发生变化')
        values = result['parameters']['values']
        if values.get('arm_control_mode') not in (1,3) or (values['arm_control_mode']==3 and values.get('arm_impedance_type')!=1):
            raise RuntimeError('当前手臂模式不在已核验的关节位置目标合同内')
        if any(values.get(side+'_gripper_type')!='omnipicker' for side in ('left','right')):
            raise RuntimeError('当前夹爪不是已核验的OmniPicker')
        result['ok'] = True
    except Exception as exc:
        result['errors'].append(f'{type(exc).__name__}: {exc}')
    return result
