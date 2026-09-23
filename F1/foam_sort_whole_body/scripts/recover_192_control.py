"""One onsite vendor recovery with SDK readback; no automatic fault-reset loop."""
import json,subprocess,time
from pathlib import Path
from recover_left_control import RecoveryBackend,PROFILE
from f1_replay.base_home import pump
from f1_replay.cli import Journal,runtime_lock,check_execution_host,wait_for_enter
from f1_replay.pose_cli import create_run
from f1_replay.runtime import ReplayError,check_controller_parameters,stop_motion

READ_SCRIPT=Path(__file__).with_name('read_arm_sdk_state.py')

def verify_state(report, *, ready, held=False):
    samples=report.get('samples',[])
    if len(samples)<2 or not all(report.get('frame_changed',{}).get(s) for s in ('left','right')):
        raise ReplayError('SDK反馈帧未更新，不能恢复')
    for sample in samples:
        for side in ('left','right'):
            arm=sample[side];state=(arm['current_state'],arm['error_code'])
            if arm['drag_type']!=0:
                raise ReplayError(f'{side}处于拖动模式，不接管')
            allowed={(1,0)} if ready or side=='right' else {(1,0),(100,2)}
            if state not in allowed:
                raise ReplayError(f'{side} SDK状态{state}，停止；不会重复清错')
            if held and max(abs(a-b) for a,b in zip(arm['sdk_input_target_deg'],arm['joint_position_deg']))>.3:
                raise ReplayError(f'{side} SDK缓存目标未保持当前反馈；不切模式')
            if held and max(abs(a-b) for a,b in zip(arm['feedback_command_deg'],arm['joint_position_deg']))>2.:
                raise ReplayError(f'{side} 控制器内部旧目标距当前姿态超过2°；不切模式')
    return samples[-1]

def sample_sdk(backend):
    process=subprocess.Popen(['sudo','-n','/usr/bin/python3',str(READ_SCRIPT)],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+4
    try:
        while True:
            try:
                stdout,stderr=process.communicate(timeout=.02)
                break
            except subprocess.TimeoutExpired:
                backend.poll(0.);backend.check(stopped=True)
                if time.monotonic()>deadline:raise ReplayError('SDK只读采样超时')
        if process.returncode:raise ReplayError('SDK只读采样失败：'+stdout[-500:]+stderr[-200:])
        return json.loads(stdout)
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=2)

def main():
    from f1_msgs.srv import SetControlMode
    profile=json.loads(PROFILE.read_text());check_execution_host(profile)
    if Path('/etc/machine-id').read_text().strip()!=profile['expected_machine_id']:
        raise ReplayError('恢复入口不属于本机')
    output=create_run();log=Journal(output/'events.jsonl');backend=RecoveryBackend(profile)
    report={'output':str(output),'sdk_ready_verified':False,'reset_attempts':0,'physical_pose_verified':False}
    enabled=False
    try:
        with runtime_lock(output):
            try:
                backend.open();pump(backend,2);backend.check(stopped=True)
                check_controller_parameters(backend.read_controller_parameters()['parameters'],profile)
                before=sample_sdk(backend);verify_state(before,ready=False)
                report['before_sdk']=before
                print('将先保持当前实测双臂，再调用系统位置模式恢复一次。该系统服务内部会清除双臂错误；不会修改保护参数。')
                print('SDK确认正常后才进入192分段起姿；若再次错误则停止，不反复清错。现场确认后按Enter：',flush=True)
                wait_for_enter(lambda:False)
                backend.poll(0.);backend.check(stopped=True)
                enabled=True;backend.enable_commands()
                hold=stop_motion(backend,profile,log);report['before_mode_stop']=hold
                if not (hold['zero_velocity_feedback_confirmed'] and hold['fresh_arm_hold_published']):
                    raise ReplayError('当前姿态保持失败，不切模式')
                held=sample_sdk(backend);verify_state(held,ready=False,held=True);report['held_sdk']=held
                if held['pid']!=before['pid']:raise ReplayError('诊断期间驱动重启，未切模式')
                if any(s['left']['current_state']!=1 or s['left']['error_code']!=0 for s in held['samples']):
                    client=backend._node.create_client(SetControlMode,'/motor_ctl/set_control_mode')
                    try:
                        if not client.wait_for_service(timeout_sec=2):raise ReplayError('系统位置模式服务不可用')
                        request=SetControlMode.Request();request.target_arm=1;request.mode=1
                        report['reset_attempts']=1
                        future=client.call_async(request);deadline=time.monotonic()+3
                        while not future.done() and time.monotonic()<deadline:
                            backend.poll(.02);backend.check(stopped=True)
                        if not future.done():raise ReplayError('恢复服务超时；状态未知，不重试')
                        response=future.result();report['service_response']={'success':response.success,'message':response.message}
                        if not response.success:raise ReplayError('系统恢复失败：'+response.message)
                    finally:backend._node.destroy_client(client)
                pump(backend,.2)
                settle_deadline=time.monotonic()+3
                while True:
                    after=sample_sdk(backend);report['after_sdk']=after
                    if after['pid']!=before['pid']:raise ReplayError('恢复期间驱动重启，停止')
                    transitioning=any(s['left']['current_state']==101 for s in after['samples'])
                    if transitioning:
                        for s in after['samples']:
                            if (s['left']['current_state'] not in (1,101) or s['left']['error_code']!=0
                                    or s['right']['current_state']!=1 or s['right']['error_code']!=0
                                    or s['left']['drag_type']!=0 or s['right']['drag_type']!=0):
                                raise ReplayError('位置模式切换中出现错误或接管；不重试')
                    else:
                        verify_state(after,ready=True,held=True)
                        if all(max(abs(a-b) for a,b in zip(s[side]['feedback_command_deg'],s[side]['joint_position_deg']))<=.3
                               for s in after['samples'] for side in ('left','right')):break
                    if time.monotonic()>settle_deadline:
                        raise ReplayError('位置模式或内部保持目标未稳定；未进入192，不重复清错')
                    pump(backend,.1)
                report['sdk_ready_verified']=True
            finally:
                if enabled:report['stop']=stop_motion(backend,profile,log)
                backend.close()
    except (Exception,KeyboardInterrupt) as exc:report['error']=f'{type(exc).__name__}: {exc}'
    finally:
        log('recovery_end',report);log.close()
        (output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if report['sdk_ready_verified'] and not report.get('error') and report.get('stop',{}).get('fresh_arm_hold_published') and report.get('stop',{}).get('zero_velocity_feedback_confirmed') else 1

if __name__=='__main__':raise SystemExit(main())
