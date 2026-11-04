"""ARM numerical owner probe: fake transport/feedback, real bounded planner.

No ROS, camera, model, SDK endpoint or real publisher is constructed. The five
measured snapshots are frozen synthetic feedback, NOT a physical validation.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','output','overlay','infra','profile'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args()
    import f1dagger
    f1dagger.__path__.insert(0,str(args.overlay))
    from f1dagger import robot_io
    from f1dagger.robot_io import make_output,F1Owner
    from f1dagger.contract import ARM_NAMES
    from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine,PrefixStopRequest
    from f1dagger.rtc_prefix_runtime import PrefixRecoveryControlLoop
    profile=json.loads(args.profile.read_text())
    bounds=np.array([profile['joint_limits_deg'][n] for n in ARM_NAMES])
    inputs=json.loads(args.inputs.read_text());results=[]
    clock=[1_000_000_000_000]
    robot_io.time=SimpleNamespace(monotonic_ns=lambda:clock[0])
    def validate(q):
        q=np.asarray(q,float)
        assert q.shape==(16,) and np.isfinite(q).all()
        assert np.all(q[:14]>=bounds[:,0]) and np.all(q[:14]<=bounds[:,1])
        for i in (0,7):assert 1.025*abs(q[i+5])+abs(q[i+6])<=110.5+1e-8
        return q.copy()
    for case in inputs:
        measured=np.asarray(case['measured16']);target=np.asarray(case['target16'])
        source=SimpleNamespace(feedback=lambda:(np.r_[measured[:14],np.zeros(8)],measured.copy(),(clock[0]-1_000_000,)*16))
        transport=SimpleNamespace(prepare=lambda _:None,release=lambda _:None,
            assert_exclusive_cached=lambda _:None,publish=lambda *_:None,
            rmw_gids={k:'FAKE-NUMERICAL-SINK' for k in ('joint_command','left_gripper_command','right_gripper_command')},own_gids={})
        owner=F1Owner(transport,source,SimpleNamespace(check_action=validate,limits=bounds),
            make_output({'_infra_root':str(args.infra)}),SimpleNamespace(check=lambda *_:None),powered=True)
        # powered=True exercises dispatch bookkeeping to the FAKE sink only.
        owner.initialize(clock[0]/1e9);owner.prepare()
        engine=object.__new__(RTCFeasiblePrefixEngine)
        engine.supervisor=SimpleNamespace(state='numerical_only',policy_epoch=0)
        engine.prefix_recovery=None;engine.tracking_hold_recovery=None
        engine.tracking_hold_recovery_enabled=True;engine.emit=lambda *_a,**_kw:None
        loop=PrefixRecoveryControlLoop(engine,owner,SimpleNamespace(),SimpleNamespace(),profile)
        req=PrefixStopRequest(0,case['request_id'],engine.prefix_owner_token,tuple(target),clock[0])
        engine.prefix_recovery={'request':req}
        first=clock[0];stationary=None
        for i in range(1500):
            clock[0]+=4_000_000
            if i%5==0:owner.approve(target,clock[0]/1e9,enabled=(False,True))
            owner.publish(clock[0]/1e9);loop.capture_stationary_publication()
            proof=loop.stop_probe(req,clock[0])
            if proof is not None and engine._command_stopped(proof,req,clock[0]):
                stationary=proof;break
        assert stationary is not None and not engine._stopped(stationary,req,clock[0])
        settle=(clock[0]-first)/1e9
        # Exercise the exact existing HOLD method, retaining other arm/grippers.
        previous=owner.last_submitted.copy();clock[0]+=4_000_000
        owner.hold_measured_right(clock[0]/1e9);owner.publish(clock[0]/1e9)
        held=owner.last_submitted.copy()
        np.testing.assert_array_equal(held[7:14],measured[7:14])
        np.testing.assert_array_equal(held[np.r_[0:7,14:16]],previous[np.r_[0:7,14:16]])
        holdreq=PrefixStopRequest(1,case['request_id'],engine.prefix_owner_token,tuple(held),clock[0])
        engine.prefix_recovery=None;engine.tracking_hold_recovery={'request':holdreq}
        loop.capture_stationary_publication();confirmed=False
        for i in range(750):
            clock[0]+=4_000_000
            if i%5==0:owner.approve(held,clock[0]/1e9,enabled=(False,False))
            owner.publish(clock[0]/1e9);loop.capture_stationary_publication()
            proof=loop.stop_probe(holdreq,clock[0])
            if proof is not None and engine._stopped(proof,holdreq,clock[0]):confirmed=True;break
        assert confirmed
        results.append(dict(episode=case['episode'],request_id=case['request_id'],
            original_command_naturally_stopped_s=settle,original_target_arrived=False,
            actual_owner_hold_matched_frozen_measured=True,post_hold_certificate_and_new_stamp_passed=confirmed))
        owner.close()
    report=dict(cases=len(results),passed=len(results),results=results,
        robot_commands=0,real_publishers=0,model_calls=0,
        limitations=['Real ARM planner and F1Owner, but fake transport, frozen feedback and simulated clock.',
            'Initial commanded v/a are zero; not a replay of full historical motion.',
            'Collision gate is a stub; numeric joint/wrist bounds checked, no geometric safety claim.',
            'Confirms HOLD bookkeeping/stop-proof integration, not physical stopping, new inference or task completion.'])
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)


if __name__=='__main__':main()
