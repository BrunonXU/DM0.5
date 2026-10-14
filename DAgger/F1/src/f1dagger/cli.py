"""Explicit inspect/shadow/teleop/run entry points; import has no ROS side effects."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
import traceback
import time
import uuid
from .config import DAGGER_ROOT, load_config, infra_import
from .contract import JointMapping, home_target, sha256


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('inspect', 'preflight', 'shadow', 'policy-shadow', 'teleop', 'run'))
    p.add_argument('--config', default=str(DAGGER_ROOT/'configs/f1_09_hil.json'))
    p.add_argument('--infra-root')
    p.add_argument('--profile')
    p.add_argument('--runtime', default=os.environ.get('F1_DAGGER_RUNTIME'))
    p.add_argument('--data-root', default=os.environ.get('F1_DAGGER_DATA_ROOT'), help='separate demonstration/policy_hil storage root')
    p.add_argument('--session-config', help='4242 next-session JSON; read once at startup')
    p.add_argument('--purpose', choices=('input_check','teleop_play','demonstration','policy_hil'))
    p.add_argument('--video', choices=('auto','on','off'), default='auto')
    p.add_argument('--task', default='F1 upper-body human correction')
    p.add_argument('--port', type=int, default=7906)
    p.add_argument('--recording-mode', choices=('hil-record', 'prebuffer-record', 'inference-only'), default='hil-record')
    p.add_argument('--execute', action='store_true', help='explicit powered mode; also requires local Enter and F710 START')
    return p.parse_args()


def main():
    args = arguments()
    config, profile = load_config(args.config, infra_root=args.infra_root, profile_path=args.profile)
    session = {}
    if args.session_config:
        from .session import read_session
        session = read_session(args.session_config)
        config['policy'].update({k:session['policy'][k] for k in ('endpoint','model_id','frequency_hz','chunk_steps')})
        args.task = session['task']
    purpose = args.purpose or session.get('purpose') or ('policy_hil' if args.mode in ('run','policy-shadow') else 'demonstration')
    if purpose == 'policy_hil' and args.mode not in ('run','policy-shadow','inspect','preflight'):
        raise ValueError('policy_hil requires run or policy-shadow')
    if args.mode in ('run','policy-shadow') and purpose != 'policy_hil':
        raise ValueError('policy modes require policy_hil purpose')
    if purpose == 'input_check' and args.mode != 'shadow':
        raise ValueError('input_check requires no-output shadow')
    if purpose == 'teleop_play':
        args.recording_mode = 'inference-only'
    video_enabled = (args.video == 'on' or (args.video == 'auto' and session.get('auto_video', purpose == 'policy_hil'))) and purpose not in ('input_check','teleop_play')
    from .identity import runtime_identity, ack_identity
    identity = runtime_identity(config, powered=args.mode in ('teleop','run'))
    print('F1 IDENTITY '+json.dumps(identity, sort_keys=True), flush=True)
    root = Path(config['_infra_root'])
    urdf = root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
    mapping = JointMapping(json.loads((root/'configs/model_mapping.json').read_text()), profile, urdf)
    mapping.check_action(home_target(profile, [0.] * 16))
    if args.mode == 'inspect':
        print(json.dumps(dict(ok=True, robot='F1-09', contract='f1_upper_16d_v1',
            policy_configured=bool(config['policy']['endpoint']), ik_verified=config['ik']['verified'],
            f710_home=profile.get('f710_home'),
            dependencies={name: importlib.util.find_spec(name) is not None for name in ('numpy', 'PIL', 'pinocchio', 'pink', 'ruckig', 'mujoco', 'yaml')},
            hardware_initialized=False, infra_manifest=config['_infra_manifest_sha256']), indent=2))
        return 0
    powered = args.mode in ('teleop', 'run')
    if powered and not args.execute:
        raise ValueError('powered modes require --execute; use shadow for zero publication')
    if powered:
        for key in ('motion_enabled', 'joints_enabled', 'limits_verified', 'control_contract_verified', 'gripper_verified'):
            if profile.get(key) is not True:
                raise ValueError('F1 machine profile gate is closed: ' + key)
        if config['ik']['verified'] is not True:
            raise ValueError('F1 Pink configuration is not yet verified; complete offline IK and real-feedback shadow before enabling it')
    if args.mode in ('run','policy-shadow') and not config['policy']['endpoint']:
        raise ValueError('F1 upper16 model endpoint is not configured; use teleop/shadow')
    verification = infra_import(root, 'robot.runtime_contract').verify_runtime_contract(profile)
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    if not verification['ok']:
        raise RuntimeError('F1 runtime contract failed; no command endpoints created')
    if args.mode == 'preflight':
        return 0
    if not args.runtime:
        raise ValueError('set F1_DAGGER_RUNTIME to a separate runtime under /home/user/Bruno')
    runtime = Path(args.runtime).resolve()
    if powered and Path('/home/user/Bruno') not in runtime.parents:
        raise ValueError('robot runtime must remain under /home/user/Bruno')
    from .robot_io import ControlLock, CollisionGate, F1Owner, make_output
    from .observation import RosObservationSource, make_camera_store, CAMERA_IDENTITY
    from .teleop import F1ArmTarget
    from .recording import F1Recorder, make_sink
    from .policy import HttpPolicy, F1PolicyEngine
    from .network import IntentServer
    from .runtime import ControlLoop, receiver_status
    control_lock = ControlLock(os.environ.get('F1_CONTROL_LOCK')) if powered else None
    transport = source = owner = engine = recorder = sink = server = status_publisher = episodes = None
    status = 'completed'
    stop = threading.Event()
    previous_handlers = {}
    run_id = datetime.now(timezone.utc).strftime('f1_hil_%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8]
    from .lifecycle import session_root, publish_completion
    episode_root = session_root(runtime, args.data_root, purpose)
    # Diagnostics/model assets belong to the process, not a training episode.
    start_bounded = purpose == 'demonstration' and args.recording_mode != 'inference-only'
    run = (runtime/'sessions' if start_bounded else episode_root)/run_id
    binding = dict(purpose=purpose, recording_mode=args.recording_mode)
    try:
        if control_lock:
            control_lock.acquire()
        adapter = infra_import(root, 'robot.adapter')
        store = make_camera_store(adapter.StateStore)
        transport = adapter.RosTransport(profile, store)
        transport.start()
        source = RosObservationSource(store, profile, config)
        deadline = time.monotonic()+5
        while True:
            try:
                full, state, _ = source.feedback()
                for key in ('joint_command', 'left_gripper_command', 'right_gripper_command'):
                    transport.assert_exclusive(key)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        mapping.check_action(state)
        run.mkdir(parents=True)
        (run/'preflight.json').write_text(json.dumps(verification, indent=2))
        gate = CollisionGate(config, mapping, run/'model_runtime', full)
        arm = F1ArmTarget(urdf, mapping, config['ik'])
        owner = F1Owner(transport, source, mapping, make_output(config), gate, powered=powered)
        full = owner.initialize(time.monotonic())
        warmup = arm.warmup(full, owner.last_submitted)
        (run/'ik_warmup.json').write_text(json.dumps(warmup, indent=2))
        policy = None
        if args.mode in ('run','policy-shadow'):
            config['policy']['require_capabilities'] = True
            policy = HttpPolicy(config['policy'], args.task, mapping.check_action)
            policy.health()
        binding = dict(runtime_identity=identity, config_sha256=config['_config_sha256'], profile_sha256=config['_profile_sha256'],
            infra_manifest_sha256=config['_infra_manifest_sha256'], model_id=None if policy is None else policy.model_id,
            execution_mode='powered' if powered else 'shadow', robot='F1-09',
            ik_verified=config['ik']['verified'], recording_mode=args.recording_mode,
            capture_hz=config['capture_hz'], policy_frequency_hz=config['policy']['frequency_hz'],
            camera_identity_source=CAMERA_IDENTITY, purpose=purpose, auto_video=video_enabled,
            f710_home=profile.get('f710_home'),
            session_config=session, task=args.task, model_version=None if policy is None else policy.model_version)
        if start_bounded:
            from .demonstration import DemonstrationRecorder
            binding.update(recording_boundary='f710_start_v1', process_run_id=run_id,
                           process_diagnostics_path=str(run))
            from .recording_audio import RecordingAudio
            episodes = DemonstrationRecorder(episode_root, runtime, binding, args.task,
                audio=RecordingAudio(runtime/'recording_audio.json') if powered else None)
            recorder = sink = episodes
        else:
            recorder = F1Recorder(run/'journal', episode_id=run_id, task_prompt=args.task, binding=binding)
            sink = make_sink(run/'payload', args.recording_mode)
        def emit(event, **fields):
            now = fields.pop('monotonic_ns', time.monotonic_ns())
            if not recorder.record_event(event, monotonic_ns=now, **fields):
                raise RuntimeError('event journal overflow')
        if powered:
            input('F1 upper16: local operator ready. Press Enter, then use F710 START to begin. ')
        engine = F1PolicyEngine(emit, source, policy, recorder, sink, config,
            teleop_only=args.mode not in ('run','policy-shadow'), episodes=episodes)
        loop = ControlLoop(engine, owner, arm, recorder, profile)
        execution_mode = 'powered' if powered else 'shadow'
        server = IntentServer(args.port, execution_mode=execution_mode, identity=ack_identity(identity))
        from .status import StatusPublisher
        status_publisher = StatusPublisher(runtime/'status.json', engine, sink, binding, run_id, finish_event=stop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        print(f'F1 {execution_mode.upper()} ({args.mode}): 127.0.0.1:{args.port}; waiting for START; raw={run}', flush=True)
        print('F1 SHADOW: no robot commands; START changes the reported input mode only.' if not powered
              else 'F1 POWERED: command output requires a fresh START and all runtime gates.', flush=True)
        if episodes:
            print('人工示教：START 开始一条；释放 RB、等待夹爪/Home 完成后再按 START 结束并保存。保存完成后 START 开始下一条；Ctrl+C 退出。', flush=True)
        while not stop.is_set():
            started = time.monotonic_ns()
            incoming, reason = server.snapshot(receiver_status(engine))
            loop.step(started, incoming, reason)
            status_publisher.update(incoming, getattr(arm,'last_target_limit_reason',None))
            stop.wait(max(0., .004-(time.monotonic_ns()-started)/1e9))
    except KeyboardInterrupt:
        pass
    except BaseException:
        status = 'failed'
        print(f'F1 FAILED: receiver is stopping; raw={run}; failure={run / "failure.txt"}',
              file=sys.stderr, flush=True)
        diagnostics = getattr(sink, 'diagnostics', None)
        if callable(diagnostics):
            try:
                print('F1 RECORDING ' + json.dumps(diagnostics(), ensure_ascii=False),
                      file=sys.stderr, flush=True)
            except Exception:
                pass  # Diagnostics must never obscure the original failure.
        if source:
            try:
                print('F1 CAMERAS ' + json.dumps(source.diagnostics(), ensure_ascii=False), file=sys.stderr, flush=True)
            except Exception:
                pass
        if run.is_dir():
            try:
                (run/'failure.txt').write_text(traceback.format_exc())
            except OSError:
                pass  # Keep the original exception if storage itself failed.
        raise
    finally:
        # Close every resource even if one cleanup fails. A failed payload
        # close cannot publish a successfully completed raw journal.
        errors = []
        try:
            if not episodes:
                publish_completion(runtime, run, binding, 'finalizing')
        except OSError:
            pass
        def cleanup(function):
            try:
                return function()
            except Exception as exc:
                errors.append(str(exc))
                return False
        if status_publisher:
            cleanup(lambda: status_publisher.close(failed=status=='failed'))
        if owner:
            cleanup(owner.close)
        if server:
            cleanup(server.close)
        if source:
            cleanup(source.stop_capture)
        if engine:
            cleanup(engine.close)
        elif source:
            source.stop_event.set()
        if transport:
            cleanup(transport.close)
        if control_lock:
            cleanup(control_lock.close)
        if source and run.is_dir():
            cleanup(lambda: (run/'camera_diagnostics.json').write_text(json.dumps(source.diagnostics(), indent=2)))
        if errors:
            status = 'failed'
        if sink and not cleanup(lambda: sink.close(status=status)):
            status = 'failed'
        if recorder and recorder is not episodes and not cleanup(lambda: recorder.close(status=status)):
            errors.append('journal not published as complete')
        if status == 'completed' and not errors and recorder and not episodes:
            # Queue creation is after commit and outside the control lifecycle.
            try:
                from .interventions import enqueue_session
                enqueue_session(run, runtime/'video-jobs')
            except Exception as exc:
                print('F1 VIDEO queue pending recovery: '+str(exc), file=sys.stderr)
        try:
            if not episodes:
                result = publish_completion(runtime, run, binding, 'saved' if status == 'completed' and not errors and recorder else 'failed', errors)
                print('F1 SESSION '+json.dumps(result, ensure_ascii=False), flush=True)
        except OSError as exc:
            errors.append('completion status: '+str(exc))
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        if errors:
            print('F1 cleanup incomplete: ' + '; '.join(errors) + f'; raw={run}',
                  file=sys.stderr, flush=True)
    return 1 if status == 'failed' or errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
