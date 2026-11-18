"""Native probes, services and an explicit local owner entry; no planner or synthetic START."""
from pathlib import Path
import argparse
import hashlib
import importlib
import json
import time
import threading
import uuid
import os
import sys


def check():
    import platform
    modules={}
    for name in ('numpy','scipy','pydantic','cv2','httpx','rclpy','sensor_msgs.msg',
                 'f1_harness.control','f1_harness.gateway','f1_harness.skill_services',
                 'f1dagger.harness_runtime','f1dagger.harness_compute','dagger_core'):
        try:
            module=importlib.import_module(name)
            modules[name]=dict(available=True,version=getattr(module,'__version__',None),
                               source=getattr(module,'__file__',None))
        except ImportError as exc:modules[name]=dict(available=False,error=type(exc).__name__)
    return dict(python=platform.python_version(),architecture=platform.machine(),modules=modules,
                imports_ready=all(v['available'] for v in modules.values()),command_publishers=0,motion_authorized=False)


def readonly_probe(config,store,seconds):
    from .evidence import FrameCache
    from .ros_observer import RosObserver
    from .engine import TaskEngine
    from .gateway import Gateway,GatewayServer
    from rpent.utils.rpc.http_rpc import HttpRpcClient
    observer=RosObserver(config,FrameCache())
    thread=threading.Thread(target=observer.spin,daemon=True);thread.start()
    engine=TaskEngine(store,mode='read_only')
    gateway=GatewayServer(Gateway(engine,observer,store)).start()
    rpc=HttpRpcClient(gateway.url,enable_sessions=False)
    try:
        deadline=time.monotonic()+seconds
        samples=0;latest=None
        while time.monotonic()<deadline:
            latest=rpc.call('observe',kwargs={},timeout_s=2)
            samples+=1;time.sleep(.1)
        capabilities=rpc.call('capabilities',kwargs={},timeout_s=2)
        frames=latest['frames'] if latest else []
        return dict(capabilities=capabilities,samples=samples,frames=[dict(camera=f['camera'],stream=f['stream'],
            width=f['width'],height=f['height'],sequence=f['sequence'],encoding=f['encoding']) for f in frames],
            pairings=latest['pairings'] if latest else [],faults=latest['faults'] if latest else [],
            command_publishers=0,motion_authorized=False,geometry_admitted=False)
    finally:
        gateway.close();observer.close();thread.join(timeout=2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('check')
    site_check=sub.add_parser('site-check');site_check.add_argument('site',type=Path)
    boot_check=sub.add_parser('boot-recheck');boot_check.add_argument('site',type=Path)
    boot_worker=sub.add_parser('boot-worker');boot_worker.add_argument('site',type=Path)
    numerical_pool=sub.add_parser('numerical-pool');numerical_pool.add_argument('site',type=Path)
    numerical_pool.add_argument('--socket',type=Path,required=True)
    probe=sub.add_parser('readonly-probe');probe.add_argument('config',type=Path)
    probe.add_argument('--seconds',type=float,default=5)
    service=sub.add_parser('serve');service.add_argument('kind',choices=['numerics','plan','verify','gateway','masks'])
    service.add_argument('config',type=Path)
    service.add_argument('--numerical-pool',type=Path)
    owner=sub.add_parser('owner');owner.add_argument('site',type=Path)
    owner.add_argument('--mode',choices=('shadow','hardware'),default='shadow')
    owner.add_argument('--web-control',action='store_true',help='explicit browser-authorized Harness owner; no gamepad')
    group=sub.add_parser('services');group.add_argument('site',type=Path)
    group.add_argument('--mode',choices=('shadow','hardware'),default='shadow')
    group.add_argument('--role',choices=('robot','perception'),default='robot')
    group.add_argument('--numerical-pool',type=Path)
    prepare=sub.add_parser('prepare-native');prepare.add_argument('site',type=Path)
    prepare.add_argument('--infra-root',type=Path,required=True)
    prepare.add_argument('--profile',type=Path,required=True)
    prepare.add_argument('--control-lock',type=Path,required=True)
    prepare.add_argument('--owner-port',type=int,required=True)
    args=parser.parse_args()
    if args.run.exists():parser.error('run directory must be new')
    from .evidence import EvidenceStore
    store=EvidenceStore(args.run)
    store.event('native_run_identity',instance_id=uuid.uuid4().hex,command=args.command,
                owner_mode=getattr(args,'mode',None))
    try:
        if args.command=='check':result=check()
        elif args.command=='site-check':
            from .readiness import inspect_site
            from .site import owner_arguments
            result=inspect_site(args.site)
            if result['static_configuration_complete']:
                argv,lock=owner_arguments(args.site,mode='hardware')
                from f1dagger.config import load_config
                from f1dagger.identity import runtime_identity
                launch=json.loads((args.site/'launch.local.json').read_text())
                controller,_=load_config(Path(launch['controller_config']),infra_root=Path(launch['infra_root']),
                                         profile_path=Path(launch['profile']))
                result['controller_identity']=runtime_identity(controller,powered=True)
                result.update(owner_arguments=argv,control_lock=lock,owner_started=False,
                              command_publishers=0,motion_authorized=False)
        elif args.command=='boot-recheck':
            from .boot_worker import refresh_boot_site
            result=refresh_boot_site(args.site)
        elif args.command=='boot-worker':
            from .boot_worker import serve_boot_worker
            serve_boot_worker(args.site)
            result=dict(boot_worker_closed=True,command_publishers=0,motion_authorized=False)
        elif args.command=='numerical-pool':
            from .numerical_pool import serve_pool
            serve_pool(args.site,args.socket,store,bundle=Path(__file__).resolve().parents[2])
            result=dict(numerical_pool_closed=True,command_publishers=0,motion_authorized=False)
        elif args.command=='readonly-probe':
            if not 0<args.seconds<=60:raise ValueError('probe_duration_out_of_bounds')
            result=readonly_probe(json.loads(args.config.read_text()),store,args.seconds)
        elif args.command=='prepare-native':
            from .site import prepare_native
            result=prepare_native(args.site,bundle=Path(__file__).resolve().parents[2],
                infra_root=args.infra_root,profile=args.profile,control_lock=args.control_lock,
                owner_port=args.owner_port,run=args.run)
        elif args.command=='services':
            import signal
            from .site import service_commands,supervise
            processes=args.run/'processes'
            commands=service_commands(args.site,bundle=Path(__file__).resolve().parents[2],
                python=sys.executable,run=processes,role=args.role,mode=args.mode,numerical_pool=args.numerical_pool)
            previous=signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            # Each service constructs its own immutable configuration and IPC
            # endpoint. Clients connect on demand; the scene worker already
            # retries unavailable dependencies. Wait for every service before
            # reporting readiness, while overlapping their cold imports/models.
            try:result=supervise(commands,processes,parallel=True)
            finally:signal.signal(signal.SIGTERM,previous)
        elif args.command=='owner':
            from .site import owner_arguments
            argv,control_lock=owner_arguments(args.site,mode=args.mode,web_control=args.web_control)
            from .mesh_cache import register_mesh_cache
            register_mesh_cache(args.site,store)
            # Only the reviewed bundle's owner can be selected by this wrapper.
            import f1dagger
            expected=Path(__file__).resolve().parents[2]/'control/src/f1dagger'
            if Path(f1dagger.__file__).resolve().parent!=expected:
                raise ValueError('owner_requires_bundled_control_source')
            from f1dagger.cli import main as owner_main
            old_argv=sys.argv[:];old_lock=os.environ.get('F1_CONTROL_LOCK')
            try:
                os.environ['F1_CONTROL_LOCK']=control_lock
                sys.argv=['f1dagger']+argv
                entry_timing=getattr(sys,'_f1_native_entry_timing',None)
                if args.web_control and entry_timing:
                    print('F1 NATIVE STARTUP '+json.dumps(dict(schema='f1-native-startup-v1',
                        source_integrity_s=entry_timing['source_integrity_s'],
                        native_import_s=entry_timing['native_import_s'],
                        entry_to_owner_main_s=time.monotonic()-entry_timing['started'])),flush=True)
                code=owner_main()
            finally:
                sys.argv=old_argv
                if old_lock is None:os.environ.pop('F1_CONTROL_LOCK',None)
                else:os.environ['F1_CONTROL_LOCK']=old_lock
            result=dict(owner_exit_code=code,mode=args.mode,
                        physical_results='inspect_owner_run_evidence')
        else:
            from .services import serve
            result=serve(args.kind,args.config,store,numerical_pool=args.numerical_pool)
        store.write_json('result.json',result)
        print(json.dumps(result,ensure_ascii=False))
        return result.get('owner_exit_code',0 if result.get('imports_ready',True) and
                          result.get('static_configuration_complete',True) else 2)
    except Exception as exc:
        result=dict(error=type(exc).__name__,reason=str(exc),
                    owner_mode=getattr(args,'mode',None))
        store.write_json('failure.json',result);print(json.dumps(result));return 1


if __name__=='__main__':raise SystemExit(main())
