#!/usr/bin/env python3
"""Wall-clock rehearsal using real timestamps and in-memory transport; no ROS."""
from pathlib import Path
import argparse,json,sys,threading,time
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'tests')]
from test_pose_action import setup
from test_ros_adapter import joints,Transport
from robot.replay_data import read_recording,ARMS,CONTEXT


def main():
    p=argparse.ArgumentParser();p.add_argument('recording');p.add_argument('--end',type=float);args=p.parse_args()
    segment=read_recording(args.recording).select(0,args.end)
    a,_,t=setup();a.clock=time.monotonic;a.store.clock=time.monotonic;a._started_at=time.monotonic()-10
    a.profile.update(profile_sha256=segment.recording.profile_sha256,robot_id=segment.recording.robot)
    a.store.update_joints(joints(list(ARMS+CONTEXT),list(segment.commands[0].values[:14]+segment.context)))
    for i,side in enumerate(('left','right')):a.store.update_gripper(side,joints([side+'_gripper_position'],[segment.commands[0].values[14+i]]))
    def refresh(key):
        Transport.assert_exclusive(t,key)
        a.store.receipt_seq+=1
        for item in list(a.store.joints.values())+list(a.store.grippers.values()):item['receipt_seq']=a.store.receipt_seq
    t.assert_exclusive=refresh;t.assert_exclusive_cached=refresh
    from robot.replay_action import execute
    output=Path('/tmp')/('f1-replay-timing-'+str(time.monotonic_ns())+'.json')
    error=None
    try:execute(a,dict(_segment=segment,replay={'episode_id':'timing-only'},_report_path=str(output)),threading.Event())
    except Exception as exc:error=str(exc)
    report=json.loads(output.read_text());report['result'].pop('replay',None);report['result'].pop('arrival_feedback',None)
    print(json.dumps(dict(result=report['result'],error=error,no_ros_loaded='rclpy' not in sys.modules,transport='in_memory_only',comparison_available=report['comparison']['available']),ensure_ascii=False,indent=2))
    return 0 if report['result']['status']=='succeeded' else 1

if __name__=='__main__':raise SystemExit(main())
