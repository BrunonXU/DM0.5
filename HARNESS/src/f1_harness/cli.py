"""Inspectable entry points; no command silently grants physical motion."""
from pathlib import Path
import argparse
import json
import threading
import time
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="new evidence directory")
    parser.add_argument("--config",type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("offline")
    offline.add_argument("--fail", action="append", default=[], choices=["grasp", "insert"])
    offline.add_argument("--planner", choices=["baseline", "astra"], default="baseline")
    offline.add_argument("--experience-snapshot",type=Path)
    remote=sub.add_parser('run-task')
    remote.add_argument('task',type=Path)
    remote.add_argument('--workspace-config',type=Path)
    remote.add_argument('--authorization-source',choices=('physical','web'),default='physical',
                        help='Expected owner event source; this flag never grants authority')
    remote.add_argument('--gateway',required=True)
    remote.add_argument('--image-gateway',required=True,help='separate loopback image tunnel for run evidence')
    remote.add_argument('--start-timeout',type=float,default=300)
    remote.add_argument('--planner',choices=['baseline','astra','vla_astra'],default='baseline')
    remote.add_argument('--experience-snapshot',type=Path)
    sub.add_parser("sandbox-probe")
    sub.add_parser("api-smoke")
    sub.add_parser("doctor")
    readiness=sub.add_parser('readiness');readiness.add_argument('directory',type=Path)
    site_init=sub.add_parser('site-init');site_init.add_argument('directory',type=Path)
    site_init.add_argument('--runtime-root',type=Path,required=True)
    report=sub.add_parser('report');report.add_argument('directory',type=Path)
    seal=sub.add_parser('seal');seal.add_argument('planner_run',type=Path)
    seal.add_argument('--owner-run',type=Path,required=True)
    seal.add_argument('--output',type=Path,required=True)
    video=seal.add_mutually_exclusive_group()
    video.add_argument('--video-root',type=Path)
    video.add_argument('--render-video',action='store_true')
    shadow=sub.add_parser('shadow-probe');shadow.add_argument('task',type=Path)
    shadow.add_argument('--gateway',required=True)
    shadow.add_argument('--skill',default='approach_grasp')
    service=sub.add_parser("serve")
    service.add_argument("kind",choices=['gateway','plan','verify','numerics','masks'])
    service.add_argument("service_config",type=Path)
    capture=sub.add_parser("inspect-capture")
    capture.add_argument("directory",type=Path)
    timing=sub.add_parser('inspect-timing')
    timing.add_argument('directory',type=Path)
    evaluation=sub.add_parser('evaluate')
    evaluation.add_argument('manifest',type=Path)
    evaluation.add_argument('runs',nargs='+',type=Path)
    memory=sub.add_parser("memory")
    memory.add_argument("operation",choices=['propose','verify','promote','snapshot'])
    memory.add_argument("root",type=Path)
    memory.add_argument("--input",type=Path)
    memory.add_argument("--id")
    memory.add_argument("--validation-run",type=Path)
    memory.add_argument("--scenario")
    memory.add_argument("--synthetic",action='store_true')
    calibration = sub.add_parser("calibrate")
    calibration.add_argument("kind", choices=["intrinsics", "handeye", "tcp", "bundle", "rigid"])
    calibration.add_argument("dataset", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    from .config import load_config
    configuration=load_config(args.config or root/'configs/planner.example.toml')
    credential_path=root/configuration['planner']['credentials_file']
    run = (args.run or root / "runs" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    if run.exists():
        parser.error("run must be a new directory; evidence is never overwritten")
    from .evidence import EvidenceStore
    store = EvidenceStore(run)
    try:
        if args.command == 'readiness':
            from .readiness import inspect_site
            result=inspect_site(args.directory)
        elif args.command == 'site-init':
            from .site import prepare_site
            result=prepare_site(args.directory,runtime_root=args.runtime_root)
        elif args.command == 'seal':
            from .evidence_pack import seal_run
            result=seal_run(args.planner_run,args.owner_run,args.output,
                            video_root=args.video_root,render_video=args.render_video)
        elif args.command == 'report':
            from .reporting import summarize_run
            result=summarize_run(args.directory)
        elif args.command == 'shadow-probe':
            from .task_runner import probe_shadow
            result=probe_shadow(args.gateway,args.task,args.skill)
        elif args.command == "serve":
            from .services import serve
            result=serve(args.kind,args.service_config,store)
        elif args.command == "inspect-capture":
            from .capture import inspect_capture
            result=inspect_capture(args.directory)
        elif args.command == 'inspect-timing':
            from .timing import inspect_timing
            result=inspect_timing(args.directory)
        elif args.command == 'evaluate':
            from .evaluation import evaluate
            result=evaluate(json.loads(args.manifest.read_text()),args.runs)
        elif args.command == "memory":
            from .memory import ExperienceStore
            memory=ExperienceStore(args.root,synthetic=args.synthetic)
            if args.operation=='snapshot':result=memory.snapshot()
            elif args.operation=='propose':result={'candidate_id':memory.propose(**json.loads(args.input.read_text()))}
            elif args.operation=='verify':result=memory.verify(args.id,validation_run=args.validation_run,scenario_id=args.scenario)
            else:result=memory.promote(args.id)
        elif args.command == 'run-task':
            from .task_runner import run_task
            if configuration['execution'].get('mode')!='hardware':
                raise ValueError('run-task_requires_explicit_hardware_transport_profile')
            result=run_task(args,store,credential_path)
        elif args.command == "doctor":
            import platform
            import shutil
            import importlib.metadata as md
            result = dict(python=platform.python_version(), bwrap=shutil.which("bwrap"),
                          dependencies={p: md.version(p) for p in ("rpent", "pyrualean", "opencv-python-headless", "mcp")},
                          credentials_available=(root / "API.MD").is_file(),
                          hardware_enabled=False)
        elif args.command == "api-smoke":
            from .planner import ResponsesPlanner
            planner = ResponsesPlanner(credential_path, store)
            try:
                result = planner.smoke()
            finally:
                planner.close()
        elif args.command == "calibrate":
            from . import calibration as cal
            data = json.loads(args.dataset.read_text())
            function = {"intrinsics": cal.solve_intrinsics, "handeye": cal.solve_handeye,
                        "tcp": cal.solve_tcp, "bundle": cal.validate_bundle,"rigid":cal.solve_rigid_alignment}[args.kind]
            result = function(**data) if args.kind != "bundle" else function(data)
        else:
            from .host import fake_host
            from .sandbox import SandboxedArm
            from .programs import SINGLE_BLOCK
            failures = {name: args.fail.count(name) for name in set(args.fail)} if args.command == "offline" else {}
            engine, plant, backend, store = fake_host(run, failures=failures)
            stop = threading.Event()
            def heartbeat():
                while not stop.wait(.25):
                    engine.heartbeat(engine.session)
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            arm = SandboxedArm(backend, run / "task")
            try:
                arm.start()
                if args.command == "sandbox-probe":
                    result = arm.audit()
                    result["tools"] = arm.get_tools_spec()
                elif args.planner == "astra":
                    from .planner import ResponsesPlanner
                    planner = ResponsesPlanner(credential_path, store)
                    try:
                        snapshot_text=''
                        if args.experience_snapshot:
                            from .memory import read_snapshot
                            snapshot=read_snapshot(args.experience_snapshot,synthetic=True)
                            snapshot_text=json.dumps(snapshot,ensure_ascii=False)
                            store.event('experience_snapshot_read',snapshot_id=args.experience_snapshot.stem.split('.')[0])
                        declared = planner.run(arm, snapshot_text=snapshot_text,task_text="Complete the already prepared single square task. Call observe first; use registered skills and finish. All data are synthetic in this validation run.")
                        result = dict(planner_declaration=declared, measured=engine.finish_task(engine.session), turns=arm.turns)
                    finally:
                        planner.close()
                else:
                    cell = arm.execute_tool("python", {"code": SINGLE_BLOCK})
                    result = dict(cell=cell.result, measured=engine.finish_task(engine.session), turns=arm.turns)
            finally:
                stop.set()
                arm.close()
        store.write_json("result.json", result)
        print(json.dumps(dict(run=str(run), result=result), ensure_ascii=False, indent=2))
        if (args.command in ('offline','run-task') and result["measured"]["outcome"] != "success"
                and not (result['measured'].get('partial_task') and result['measured'].get('goal_reached'))):
            return 2
        if args.command=='readiness' and not result['static_configuration_complete']:return 2
        if args.command=='seal' and not result['integrity_complete']:return 2
        return 0
    except Exception as exc:
        # Credentials cannot occur in these controlled error types. Do not dump tracebacks/HTTP objects.
        error = dict(type=type(exc).__name__, code=str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "operation_failed")
        from .program_reuse import ProgramOwnerFailure
        if isinstance(exc, ProgramOwnerFailure):
            error.update(skill=exc.skill, owner_result=exc.receipt.model_dump(mode='json'),
                         execution_authority_valid=False)
        store.write_json("failure.json", error)
        print(json.dumps(dict(run=str(run), failure=error), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
