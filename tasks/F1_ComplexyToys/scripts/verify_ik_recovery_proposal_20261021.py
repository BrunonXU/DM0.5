"""Offline real strict-candidate response -> actual client _proposal contract.

No engine constructor, threads, model, ROS, network or output owner is started.
Original source contexts and previous accepted chunks are retained. Receipt
clock values and verified transport identities are deliberately supplied test
fixtures; this does NOT certify historical deadlines or websocket validation.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates',type=Path,required=True)
    p.add_argument('--joined',type=Path,required=True)
    p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine
    from f1dagger.policy_errors import IKRejected
    from f1dagger.contract import JointMapping, ARM_NAMES
    from f1dagger import rtc_prefix_recovery, contract
    records=json.loads(a.candidates.read_text())['records']
    source={(r['episode'],r['server']['request_id']):r['server']
        for r in map(json.loads,a.joined.read_text().splitlines())}
    profile=json.loads(a.profile.read_text())
    # check_action only consumes the exact profile bounds. Do not load model
    # assets or pretend this isolated validator performed runtime binding.
    mapping=object.__new__(JointMapping)
    mapping.limits=np.asarray([profile['joint_limits_deg'][n] for n in ARM_NAMES],float)
    results=[];skipped_full=skipped_empty=0
    for record in records:
        if record['complete']:
            skipped_full+=1;continue
        if record['valid_new_points']==0:
            skipped_empty+=1;continue
        s=source[record['episode'],record['request_id']]
        ctx=copy.deepcopy(s['rtc_request'])
        held=np.array(s['prediction_replay']['last_submitted16'],float)
        engine=object.__new__(RTCFeasiblePrefixEngine)
        epoch=s['policy_epoch'];rid=s['request_id'];session=s['session_id']
        engine.ik_failures=0;engine.ik_retry_limit=3
        engine.real_future=None;engine.prefix_recovery=None
        engine.pending_context=ctx;engine.real_future_request_id=rid
        engine.real_future_epoch=epoch;engine.supervisor=SimpleNamespace(policy_epoch=epoch)
        engine.waypoint_hz=30.
        engine.last_requested=SimpleNamespace(observation_id=s['observation_id'],
            state16=s['prediction_replay']['state16'])
        engine.policy=SimpleNamespace(right_only=True,held_getter=lambda:held.copy(),
            validate_action=mapping.check_action,last_timing=dict(response_verified=True,
            outcome='ik_rejected',request_id=rid,policy_epoch=epoch,session_id=session))
        if ctx['bootstrap']:
            now=1_000_000_000_000
            engine.chunk=None;engine.plan_id=0;engine.chunk_started_ns=0
            expected_anchor=held.copy();expected_origin=now
        else:
            previous=source[record['episode'],ctx['base_plan_id']]
            engine.chunk=np.array(previous['actions'],float)
            engine.plan_id=ctx['base_plan_id']
            engine.chunk_started_ns=ctx['plan_origin_ns']-round(ctx['shift_steps']/30*1e9)
            np.testing.assert_array_equal(engine.chunk[ctx['shift_steps']:ctx['shift_steps']+ctx['delay_steps']],ctx['committed_actions16'])
            now=ctx['plan_origin_ns']+round(ctx['delay_steps']/30*1e9/2)
            expected_anchor=engine.chunk[ctx['shift_steps']-1].copy()
            expected_origin=ctx['plan_origin_ns']
        engine.last_request_ns=now-1_000_000
        response=copy.deepcopy(s)
        response.update(success=False,actions=None,reason='ik_rejected',
            diagnostics=copy.deepcopy(record['diagnostics']),absolute_eef10=copy.deepcopy(record['absolute_eef10']))
        exc=IKRejected(response)
        before_chunk=None if engine.chunk is None else engine.chunk.copy()
        before_context=copy.deepcopy(ctx)
        row={k:record[k] for k in ('episode','request_id','trial','valid_new_points','committed_points')}
        row.update(bootstrap=ctx['bootstrap'],receipt_time_is_synthetic=True,
            fixture_receipt_monotonic_ns=now,prior_chunk_is_historical=not ctx['bootstrap'])
        try:
            actions,absolute,anchor,origin,endpoint=engine._proposal(exc,now)
            expected=np.asarray(record['actions'],float)
            np.testing.assert_array_equal(actions,expected)
            np.testing.assert_array_equal(absolute,record['absolute_eef10'])
            np.testing.assert_array_equal(anchor,expected_anchor)
            assert origin==expected_origin
            assert endpoint==origin+round(len(expected)/30*1e9)
            assert ctx==before_context
            if before_chunk is not None:np.testing.assert_array_equal(engine.chunk,before_chunk)
            row.update(passed=True,joints_and_grippers_bitwise_equal=True,immutable_prefix_and_origin=True,
                valid_points=len(actions),max_action_error=0.,endpoint_monotonic_ns=endpoint)
        except Exception as error:
            row.update(passed=False,error_type=type(error).__name__,error=str(error))
        if not ctx['bootstrap']:
            late=ctx['plan_origin_ns']+round(ctx['delay_steps']/30*1e9)+1
            try:
                engine._proposal(exc,late)
                row['late_rejection_passed']=False
            except ValueError as error:
                row['late_rejection_passed']=str(error)=='late_prefix_adoption'
                row['late_rejection_reason']=str(error)
        results.append(row)
    summary=dict(input_records=len(records),skipped_full=skipped_full,skipped_zero_new_points=skipped_empty,
        partial_nonzero_checked=len(results),passed=sum(r['passed'] for r in results),
        bootstrap_checked=sum(r['bootstrap'] for r in results),guided_checked=sum(not r['bootstrap'] for r in results),
        guided_late_boundaries_passed=sum(r.get('late_rejection_passed',False) for r in results),
        cases_checked=len({(r['episode'],r['request_id']) for r in results}),
        all_actions_bitwise_equal=all(r.get('joints_and_grippers_bitwise_equal',False) for r in results),
        robot_commands=0,model_calls=0,network_calls=0,engine_constructors=0,
        limitations=['Synthetic receipt times deliberately fall inside the existing committed prefix; no historical deadline claim.',
            'Verified transport identities are test fixtures; this checks _proposal after verification, not websocket authenticity.',
            'No robot, output planner, stopping evidence, physical feasibility or task completion acceptance.',
            'Complete and zero-new-point candidates are excluded from partial adoption by design.'])
    a.output.mkdir(parents=True,exist_ok=False)
    (a.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    (a.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    paths=[Path(__file__),a.candidates,a.joined,a.profile,Path(rtc_prefix_recovery.__file__),Path(contract.__file__)]
    (a.output/'manifest.json').write_text(json.dumps(dict(source_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},robot_commands=0),indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    if summary['passed']!=summary['partial_nonzero_checked'] or summary['guided_late_boundaries_passed']!=summary['guided_checked']:
        raise SystemExit(1)


if __name__=='__main__':main()
