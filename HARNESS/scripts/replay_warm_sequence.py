"""Consecutive full FAKE cycles with one fixed-model grasp prefetch process.

Each cycle retains a fresh TaskEngine, owner, scene, log and authorization scope.
Only the optional speculative numeric worker survives normal task completion.
This is not a ROS/web launcher; it has no robot address or API credentials.
"""
import argparse
from copy import deepcopy
import gc,hashlib,json,runpy,sys,time,uuid
from pathlib import Path

from f1_harness.contracts import Task,digest
from warm_grasp_process import WarmGraspProcess,recipe_identity


def configuration_identity(task,fixture,spec):
    value=deepcopy(spec)
    # Mutable cache/log destination is not model configuration. All other
    # values, including native initial body state, remain part of the binding.
    value['gate'].pop('runtime',None)
    return digest(dict(recipe=recipe_identity(task,fixture),spec=value))


class ReplayWarmFactory:
    def __init__(self,worker,binding):
        self.worker=worker;self.binding=binding;self.calls=[]

    def __call__(self,*,task,fixture,spec,clock_mapping,scope):
        row=dict(scope=deepcopy(scope),physical_motion=False,production_enabled=False)
        self.calls.append(row)
        if configuration_identity(task,fixture,spec)!=self.binding:
            row.update(reused=False,reason='fixed_configuration_changed')
            return None,row
        if self.worker.cleanup is not None:
            row.update(reused=False,reason='worker_closed_no_automatic_restart')
            return None,row
        begin=time.perf_counter()
        try:
            owner=fixture.get('frame_chain',{}).get('development_owner_config')
            if not owner:raise ValueError('offline_web_binding_requires_owner_record')
            lease=self.worker.acquire(scope,clock_mapping=clock_mapping,task=task,fixture=fixture,
                identity_files=tuple(Path(owner).parent.glob('*.json')))
        except Exception as exc:
            row.update(reused=False,reason=type(exc).__name__,acquire_s=time.perf_counter()-begin)
            return None,row
        row.update(reused=True,acquire_s=time.perf_counter()-begin,pid=self.worker.process.pid,
                   serial=lease.serial,initialization_sha256=self.worker.initialization_sha256,
                   asset_id=self.worker.asset_id)
        return lease,row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--scene',type=Path,required=True)
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--freeze-web-runs',action='store_true',
        help='Exercise the real per-web-request site freezer locally; no web server or robot')
    parser.add_argument('--cases',nargs='+',choices=('fresh','nominal','late','cancel','after_cancel'),
                        default=['fresh','nominal','nominal','cancel','after_cancel'])
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    area=args.output.resolve();area.mkdir(parents=True,exist_ok=False)
    site=args.site.resolve();scene=args.scene.resolve()
    read=lambda p:json.loads(p.read_text())
    task=Task.model_validate_json((site/'task.local.json').read_text())
    fixture=read(site/'plan.local.json')['fixture']
    if fixture['frame_chain'].get('development_owner_config'):
        fixture['frame_chain']['development_owner_config']=str(site/'owner.local.json')
    spec=read(site/'numerics.local.json')['spec']
    spec['urdf']=str(root.parent/'F1-4242/web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf')
    spec['gate']['config']['_infra_root']=str(root.parent/'F1-4242')
    spec['gate']['runtime']=str(area/'warm-native-model');spec['gate']['full22']=read(scene)[0]['full22']
    scripts=('replay_warm_sequence.py','replay_full_owner.py','warm_grasp_process.py','replay_planning_process.py',
             'prepared_grasp.py','replay_grasp_prefetch.py','audit_assets.py','audit_asset_watch.py','audit_worker_launch.py')
    identity_files=[*(root/'scripts'/name for name in scripts),*sorted((root/'src/f1_harness').glob('*.py')),
                    *sorted(site.glob('*.json')),scene]
    worker=None;reports=[];failure=None
    started=time.perf_counter()
    summary=dict(schema='f1-offline-warm-grasp-sequence-v1',physical_motion=False,robot_connected=False,paid_model_calls=0,
                 reports=reports,real_experience_eligible=False,web_integration=False,
                 initial_state_reset_between_cycles=True)
    try:
        prewarm=time.perf_counter()
        worker=WarmGraspProcess(task=task,fixture=fixture,spec=spec,runtime=area/'warm-process',
            identity_files=identity_files,assets=args.assets,
            clock_mapping=lambda:dict(origin_ns=10_000_000_000,start_wall_ns=time.monotonic_ns()))
        factory=ReplayWarmFactory(worker,configuration_identity(task,fixture,spec))
        summary.update(prewarm_total_s=time.perf_counter()-prewarm,worker_startup_s=worker.startup_s,
                       asset_id=worker.asset_id,initialization_sha256=worker.initialization_sha256,
                       identity_files=worker.identities)
        for index,case in enumerate(args.cases):
            destination=area/(str(index)+'-'+case)
            run_site=site
            if args.freeze_web_runs:
                from f1_harness.web_snapshot import freeze_site
                name='web-'+uuid.uuid4().hex;run_site=area/'sites'/name
                config=dict(mode='fixed',instruction='Offline saved-scene single block cycle',
                            corrections=0 if index%2 else 2,note='Simulation only; no hardware authorization')
                revision=hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                freeze_site(site,run_site,remote_site='/home/user/Bruno/HARNESS/site/'+name,
                            request=dict(task_id='square_block',config_revision=revision,config=config),run_name=name)
            argv=[str(root/'scripts/replay_full_owner.py'),'--site',str(run_site),'--scene',str(scene),
                  '--output',str(destination),'--wall-clock','--follower','approved-output','--prefetch-grasp']
            if case=='late':argv+=['--prefetch-delay-s','5']
            if case=='cancel':argv+=['--prefetch-delay-s','1','--cancel-during-prefetch','--expect-outcome','interrupted']
            cycle_start=time.perf_counter();oldargv=sys.argv
            try:
                sys.argv=argv
                runpy.run_path(argv[0],run_name='__main__',
                    init_globals={} if case=='fresh' else {'_offline_prefetch_factory':factory})
            finally:sys.argv=oldargv
            path=Path(read(destination/'latest-report.json')['path']);report=read(path)
            row=dict(case=case,report=str(path),driver_cycle_s=time.perf_counter()-cycle_start,
                     site=str(run_site),web_site_frozen=args.freeze_web_runs,
                     ready_to_done_s=report['wall_clock_replay']['ready_to_done_s'],
                     entry_to_report_s=report['wall_clock_replay']['process_entry_to_report_s'],
                     grasp_prefetch=report.get('grasp_prefetch'),warm_setup=report.get('prewarmed_grasp_setup'),
                     checks=report['checks'])
            reports.append(row)
            if case!='fresh':
                assert row['warm_setup']['reused'] is True
            if case in ('nominal','after_cancel'):
                assert report['grasp_prefetch']['adopted']==1
                assert report['grasp_prefetch']['planner_cleanup']['worker_retained']
            if worker.active_lease is not None:raise AssertionError('previous_run_lease_still_active')
            # runpy functions form cycles with their per-run globals. Collect
            # only after all run-owned control/planning objects have closed,
            # before the next control loop. Do not disable/tune normal GC or
            # conceal this setup cost in ready-to-done timing.
            collect_begin=time.perf_counter();collected=gc.collect()
            row['between_cycles_gc']=dict(collected=collected,seconds=time.perf_counter()-collect_begin,
                                          outside_control_loop=True)
            (area/'progress.json').write_text(json.dumps(dict(reports=reports,calls=factory.calls),indent=2)+'\n')
        scopes=[r['warm_setup']['scope'] for r in reports if r['warm_setup']]
        assert len({s['session_id'] for s in scopes})==len(scopes)
        assert len({s['owner_instance_id'] for s in scopes})==len(scopes)
        summary.update(passed=True,calls=factory.calls,distinct_sessions_and_owners=True)
    except BaseException as exc:
        failure=exc;summary.update(passed=False,error_type=type(exc).__name__,reason=str(exc))
    finally:
        if worker is not None:
            worker.close();summary['worker_cleanup']=worker.cleanup
        summary['sequence_total_s']=time.perf_counter()-started
        (area/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps({k:v for k,v in summary.items() if k not in ('identity_files','reports','calls')},indent=2),flush=True)
    if failure is not None:raise failure


if __name__=='__main__':main()
