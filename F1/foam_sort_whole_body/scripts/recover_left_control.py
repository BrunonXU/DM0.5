"""Onsite recovery through existing motor_ctl services, then normal home.

No SDK second connection, driver restart, or limit change.
CAUTION: the vendor set_control_mode implementation inspected on 4F-F1 clears
both arms errors internally; this is not a mode-only diagnostic operation.
ROS parameter values do not prove the actual controller is in position mode.
"""
import json
from pathlib import Path
import sys
import time

from f1_replay.base_home import HomeBackend,pump
from f1_replay.cli import Journal,runtime_lock,check_execution_host,wait_for_enter
from f1_replay.pose_cli import create_run
from f1_replay.runtime import ReplayError,check_controller_parameters,stop_motion

PROFILE=Path('/home/user/Bruno/F1/foam_sort_whole_body/releases/20260918-4F-F1-v4/workspace/F1/foam_sort_whole_body/configs/pose.f1-29.json')


class RecoveryBackend(HomeBackend):
    """Home status subscriptions plus arm-hold and base-stop publishers only."""
    def enable_commands(self):
        from f1_replay.command_monitor import publisher_gid
        self._require_open()
        for key in ('arms_command','base_command'):
            publisher=self._node.create_publisher(self._message_types[key],
                self.profile['topics'][key],self._command_qos)
            self._publishers[key]=publisher
            self._own_command_gids[key]=publisher_gid(publisher)
        self._commands_enabled=True

    def command_activity(self):
        result=super().command_activity()
        result['own_gids_bound']=all(self._own_command_gids.get(k) for k in ('arms_command','base_command'))
        return result


def main():
    from f1_msgs.srv import SetDragMode,SetControlMode
    profile=json.loads(PROFILE.read_text())
    check_execution_host(profile)
    if Path('/etc/machine-id').read_text().strip()!=profile['expected_machine_id']:
        raise ReplayError('恢复入口不属于本机')
    output=create_run();backend=RecoveryBackend(profile);log=Journal(output/'events.jsonl')
    report={'output':str(output),'left_mode_services_completed':False,'physical_recovery_verified':False}
    enabled=False
    try:
        with runtime_lock(output):
            try:
                backend.open();pump(backend,2);backend.check(stopped=True)
                check_controller_parameters(backend.read_controller_parameters()['parameters'],profile)
                print('将保持当前实测关节，调用系统接口退出左臂拖动、恢复左臂位置控制，然后运行原home。')
                wait_for_enter(lambda:False)
                backend.poll(0.);backend.check(stopped=True)
                enabled=True;backend.enable_commands()
                report['before_mode_stop']=stop_motion(backend,profile,log)
                if not (report['before_mode_stop']['zero_velocity_feedback_confirmed']
                        and report['before_mode_stop']['fresh_arm_hold_published']):
                    raise ReplayError('未建立当前反馈保持，未切换控制模式')
                for typ,name,fields in (
                    (SetDragMode,'/motor_ctl/set_drag_mode',dict(target_arm=1,drag_type=0,duration_ms=0)),
                    (SetControlMode,'/motor_ctl/set_control_mode',dict(target_arm=1,mode=1))):
                    client=backend._node.create_client(typ,name)
                    try:
                        if not client.wait_for_service(timeout_sec=2):raise ReplayError(name+'不可用')
                        request=typ.Request()
                        for key,value in fields.items():setattr(request,key,value)
                        log('mode_request',dict(service=name,fields=fields))
                        future=client.call_async(request);deadline=time.monotonic()+3
                        while not future.done() and time.monotonic()<deadline:
                            backend.poll(.02);backend.check(stopped=True)
                        if not future.done():raise ReplayError(name+'超时，状态未知；不继续home')
                        response=future.result()
                        log('mode_response',dict(service=name,success=response.success,message=response.message))
                        if not response.success:raise ReplayError(name+': '+response.message)
                        pump(backend,.2)
                    finally:backend._node.destroy_client(client)
                report['left_mode_services_completed']=True
            finally:
                if enabled:
                    report['stop']=stop_motion(backend,profile,log)
                backend.close()
    except (Exception,KeyboardInterrupt) as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
    finally:
        log('recovery_end',report);log.close()
        (output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if (report['left_mode_services_completed'] and not report.get('error')
                 and report.get('stop',{}).get('zero_velocity_feedback_confirmed')
                 and report.get('stop',{}).get('fresh_arm_hold_published')) else 1


if __name__=='__main__':
    raise SystemExit(main())
