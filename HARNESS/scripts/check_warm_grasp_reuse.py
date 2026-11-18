"""Saved-scene native lease/latency check. No owner, outputs, robot or API."""
import argparse,json,time,sys,hashlib
from copy import deepcopy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from f1_harness.contracts import Observation,SkillIntent,Task,digest,HarnessError
from warm_grasp_process import WarmGraspProcess,SCOPE
from replay_grasp_prefetch import GraspPrefetch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();area=args.output.resolve();area.mkdir(parents=True,exist_ok=False)
    read=lambda p:json.loads(p.read_text())
    report=read(args.report);run=args.report.parent;site=Path(report['scenario']['site'])
    if report['mode']!='fake' or report['physical_motion'] is not False:raise ValueError('fake_source_required')
    original={p:h for p,h in report['source_files'].items() if '/scripts/' not in p}
    if not all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in original.items()):
        raise ValueError('saved_runtime_or_site_changed')
    events=[json.loads(line) for line in (run/'collected/owner/harness/trace.jsonl').read_text().splitlines()]
    parent=next(e for e in events if e['kind']=='harness_planning_started' and e['intent']['skill']=='approach_grasp')
    grasp=next(e for e in events if e['kind']=='harness_planning_started' and e['intent']['skill']=='grasp')
    parent_plan=next(p['proposal'] for p in read(run/'plans.json') if p['skill']=='approach_grasp')
    task=Task.model_validate_json((site/'task.local.json').read_text())
    fixture=read(site/'plan.local.json')['fixture'];fixture['frame_chain']['development_owner_config']=str(site/'owner.local.json')
    spec=deepcopy(report['offline_relocations']['numerics_spec']);spec['gate']['runtime']=str(area/'native-model')
    mapping=dict(origin_ns=parent['observation']['received_ns'],start_wall_ns=time.monotonic_ns())
    now=lambda:mapping['origin_ns']+time.monotonic_ns()-mapping['start_wall_ns']
    scripts=[Path(__file__),Path(__file__).with_name('warm_grasp_process.py'),Path(__file__).with_name('prepared_grasp.py'),
             Path(__file__).with_name('replay_planning_process.py'),Path(__file__).with_name('audit_worker_launch.py'),
             Path(__file__).with_name('audit_assets.py'),Path(__file__).with_name('audit_asset_watch.py'),
             Path(__file__).with_name('replay_grasp_prefetch.py')]
    identity_files=[*map(Path,original),*scripts]
    worker=None;rows=[];failure=None
    summary=dict(physical_motion=False,simulation=True,execution_authorized=False,source_report=str(args.report.resolve()),
                 end_to_end_timing_proven=False,source_runtime_identity=original,rows=rows)

    def scope(tag):
        intent=SkillIntent.model_validate(parent['intent']).model_copy(update=dict(session_id='simulation:'+tag,
            owner_instance_id='simulation-owner:'+tag,epoch=1))
        return {k:getattr(intent,k) for k in SCOPE}

    def prepare(lease,tag):
        intent=deepcopy(parent['intent']);intent.update(scope(tag))
        return lease.dispatch('prepare_grasp',dict(key=tag,parent_intent=intent,parent_proposal=parent_plan,
            observation=parent['observation'],full22=parent['full_reference']))

    def adoption_args(tag,key=None):
        stamp=now();obs=Observation.model_validate(grasp['observation'])
        obs=obs.model_copy(update=dict(observation_id='simulation:warm:'+tag,scene_version='simulation:warm:'+tag,
            received_ns=stamp,state_received_ns=stamp,
            frames=tuple(f.model_copy(update=dict(received_ns=stamp,source_ns=stamp)) for f in obs.frames)))
        intent=SkillIntent.model_validate(grasp['intent']).model_copy(update=dict(**scope(tag),
            request_id='simulation:warm:'+tag,observation_id=obs.observation_id,scene_version=obs.scene_version))
        return dict(key=key or tag,intent=intent.model_dump(),observation=obs.model_dump(),
            full22=grasp['full_reference'],held16=grasp['held_reference'])

    def adopt(lease,tag,key=None):return lease.dispatch('adopt_grasp',adoption_args(tag,key))

    try:
        begin=time.perf_counter()
        worker=WarmGraspProcess(task=task,fixture=fixture,spec=spec,runtime=area/'worker',assets=args.assets,
            identity_files=identity_files,clock_mapping=lambda:dict(mapping))
        summary.update(total_constructor_s=time.perf_counter()-begin,worker_startup_s=worker.startup_s,
                       pid=worker.process.pid,asset_id=worker.asset_id,initialization_sha256=worker.initialization_sha256,
                       source_files=worker.identities)
        # Preserve running wall clock throughout native calculation. These are
        # declared synthetic scenes, not redated physical observations.
        outputs=[]
        for tag in ('abandoned','second','third'):
            begin=time.perf_counter();lease=worker.acquire(scope(tag));acquire_s=time.perf_counter()-begin
            stale_rejected=False
            if tag=='second':
                try:adopt(lease,tag,key='abandoned');raise AssertionError('old_candidate_survived')
                except HarnessError as exc:
                    assert 'prepared_candidate_missing' in str(exc);stale_rejected=True
            begin=time.perf_counter();prepared=prepare(lease,tag);prepare_s=time.perf_counter()-begin
            result=None;adopt_s=None
            if tag!='abandoned':
                begin=time.perf_counter();result=adopt(lease,tag);adopt_s=time.perf_counter()-begin
                outputs.append({k:result['proposal'][k] for k in ('targets','trajectory','geometry_trace')})
                (area/(tag+'-proposal.json')).write_text(json.dumps(result,indent=2)+'\n')
            begin=time.perf_counter();lease.close();release_s=time.perf_counter()-begin
            rows.append(dict(run=tag,acquire_s=acquire_s,prepare_s=prepare_s,adopt_s=adopt_s,release_s=release_s,
                pid=worker.process.pid,child_alive=worker.process.poll() is None,old_candidate_rejected=stale_rejected,
                candidate_sha256=prepared['source_sha256'],adopted=result is not None))
        summary['second_third_targets_and_schedule_identical']=all(outputs[0][k]==outputs[1][k] for k in ('targets','trajectory'))
        assert summary['second_third_targets_and_schedule_identical']
        assert all(r['child_alive'] for r in rows)
        # Use the same asynchronous coordinator as the complete fake replay.
        # The saved parent succeeded; this check produces plans only, no owner.
        tag='coordinator';lease=worker.acquire(scope(tag))
        current=lambda intent:all(getattr(intent,k)==v for k,v in scope(tag).items())
        coordinator=GraspPrefetch(lease,current,current)
        try:
            intent=SkillIntent.model_validate(parent['intent']).model_copy(update=scope(tag))
            coordinator.start(intent,Observation.model_validate(parent['observation']),parent['full_reference'],parent_plan)
            coordinator.future.result(timeout=3)
            kwargs=adoption_args(tag)
            proposal=coordinator.take(SkillIntent.model_validate(kwargs['intent']),Observation.model_validate(kwargs['observation']),
                                      kwargs['full22'],kwargs['held16'])
            assert proposal is not None and proposal['targets']==outputs[0]['targets']
            (area/'coordinator.json').write_text(json.dumps(coordinator.records,indent=2)+'\n')
        finally:coordinator.close()
        summary['coordinator']=dict(adopted=True,old_targets_identical=True,
            lease_closed=lease.closed,worker_alive_after_close=worker.process.poll() is None)
        assert summary['coordinator']['worker_alive_after_close']
        # A busy lease is deliberately not reused; closing its socket/process
        # makes the outstanding result unconsumable, with explicit cold restart.
        lease=worker.acquire(scope('cancel'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(prepare,lease,'cancel')
            deadline=time.monotonic()+1
            while not lease.busy and not future.done() and time.monotonic()<deadline:time.sleep(.001)
            assert lease.busy,'native_cancel_window_not_observed'
            begin=time.perf_counter();lease.close();cancel_s=time.perf_counter()-begin
            try:future.result(timeout=3);raise AssertionError('cancelled_result_returned')
            except (RuntimeError,ConnectionError,OSError,HarnessError) as exc:
                summary['cancel']=dict(result_discarded=True,error_type=type(exc).__name__,close_s=cancel_s)
        try:worker.acquire(scope('after-cancel'));raise AssertionError('cancelled_process_reused')
        except RuntimeError as exc:assert str(exc)=='warm_grasp_process_closed'
        summary['passed']=True
    except BaseException as exc:
        failure=exc;summary.update(passed=False,error_type=type(exc).__name__,reason=str(exc))
    finally:
        if worker is not None:
            worker.close();summary['cleanup']=worker.cleanup
        (area/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps({k:v for k,v in summary.items() if k not in ('source_files','source_runtime_identity')},indent=2))
    if failure is not None:raise failure


if __name__=='__main__':main()
