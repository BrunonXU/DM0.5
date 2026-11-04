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
    p.add_argument('mode', choices=('inspect', 'preflight', 'shadow', 'policy-shadow', 'teleop', 'run', 'sync', 'sync-shadow'))
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
    timing = p.add_mutually_exclusive_group()
    timing.add_argument('--duration-seconds', type=float, help='sync wall-clock duration after local Enter; default 60s')
    timing.add_argument('--continuous', action='store_true', help='sync until Ctrl+C or fault')
    p.add_argument('--rtc', action='store_true', help='inference-time RTC on standalone entry')
    p.add_argument('--episode-controls', action='store_true', help='START begins, D-pad UP saves independent episodes')
    p.add_argument('--rtc-hil', action='store_true', help='RTC with real F710 START takeover/handoff')
    p.add_argument('--rlt-hil', action='store_true', help='START begins/takes over; D-pad right selects local RLT; no Enter prompt')
    p.add_argument('--rtc-delay-steps', type=int, default=15)
    p.add_argument('--rtc-min-steps', type=int, default=20)
    p.add_argument('--execute', action='store_true', help='explicit powered mode; RLT waits for F710 START; other routes retain local Enter')
    args = p.parse_args()
    if args.rlt_hil and (args.rtc or args.rtc_hil or args.mode not in ('run', 'policy-shadow') or not args.episode_controls):
        p.error('--rlt-hil requires run/policy-shadow --episode-controls and excludes --rtc/--rtc-hil')
    if args.rlt_hil and args.recording_mode != 'hil-record':
        p.error('--rlt-hil requires full hil-record')
    if args.rtc_hil and (args.rtc or args.mode not in ('run', 'policy-shadow')):
        p.error('--rtc-hil requires run/policy-shadow and is separate from --rtc')
    if args.rtc_hil and args.recording_mode != 'hil-record':
        p.error('--rtc-hil requires full hil-record')
    if args.rtc and args.mode not in ('sync', 'sync-shadow'):
        p.error('--rtc requires standalone sync/sync-shadow')
    if (args.rtc or args.rtc_hil or args.rlt_hil) and not 1 <= args.rtc_delay_steps <= args.rtc_min_steps <= 49-args.rtc_delay_steps:
        p.error('RTC requires 1 <= d <= s_min < 50-d')
    import math
    if args.duration_seconds is not None and (not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0):
        p.error('--duration-seconds must be positive and finite')
    if (args.duration_seconds is not None or args.continuous) and args.mode not in ('sync','sync-shadow') and not (args.rtc_hil or args.rlt_hil):
        p.error('duration options require sync or sync-shadow')
    return args


def main():
    args = arguments()
    hil_policy = args.rtc_hil or args.rlt_hil
    from .shadow_recording import SHADOW_MODES, ShadowRecorder, workspace
    shadow_run = args.mode in SHADOW_MODES
    if shadow_run:
        args.recording_mode = "inference-only"
        args.video = "off"
    standalone = args.mode in ('sync','sync-shadow')
    from .standalone import StandalonePolicyEngine, StandaloneFinished, LocalResumeInput
    config, profile = load_config(args.config, infra_root=args.infra_root, profile_path=args.profile)
    session = {}
    if args.session_config:
        from .session import read_session
        session = read_session(args.session_config)
        config['policy'].update({k:session['policy'][k] for k in ('endpoint','model_id','frequency_hz','chunk_steps')})
        args.task = session['task']
    purpose = args.purpose or session.get('purpose') or ('policy_hil' if args.mode in ('run','policy-shadow','sync','sync-shadow') else 'demonstration')
    if purpose == 'policy_hil' and args.mode not in ('run','policy-shadow','sync','sync-shadow','inspect','preflight'):
        raise ValueError('policy_hil requires run or policy-shadow')
    if args.mode in ('run','policy-shadow','sync','sync-shadow') and purpose != 'policy_hil':
        raise ValueError('policy modes require policy_hil purpose')
    if purpose == 'input_check' and args.mode != 'shadow':
        raise ValueError('input_check requires no-output shadow')
    if purpose == 'teleop_play':
        args.recording_mode = 'inference-only'
    if args.episode_controls and purpose == 'policy_hil' and not hil_policy:
        raise ValueError('policy episodes require explicit RTC-HIL')
    video_enabled = (args.video == 'on' or (args.video == 'auto' and session.get('auto_video', purpose == 'policy_hil'))) and purpose not in ('input_check','teleop_play')
    from .identity import runtime_identity, ack_identity
    identity = runtime_identity(config, powered=args.mode in ('teleop','run','sync'))
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
    powered = args.mode in ('teleop', 'run', 'sync')
    if powered and not args.execute:
        raise ValueError('powered modes require --execute; use shadow for zero publication')
    if powered:
        for key in ('motion_enabled', 'joints_enabled', 'limits_verified', 'control_contract_verified', 'gripper_verified'):
            if profile.get(key) is not True:
                raise ValueError('F1 machine profile gate is closed: ' + key)
        if config['ik']['verified'] is not True:
            raise ValueError('F1 Pink configuration is not yet verified; complete offline IK and real-feedback shadow before enabling it')
    if args.mode in ('run','policy-shadow','sync','sync-shadow') and not config['policy']['endpoint']:
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
    hil_feedback = None
    rlt_policy = None
    manual_planner = None
    status = 'completed'
    stop = threading.Event()
    termination = {'reason': 'initialization_incomplete', 'signal': None}
    previous_handlers = {}
    run_id = datetime.now(timezone.utc).strftime('f1_hil_%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8]
    from .lifecycle import session_root, publish_completion
    episode_root = session_root(runtime, args.data_root, purpose)
    # Diagnostics/model assets belong to the process, not a training episode.
    start_bounded = (purpose == 'demonstration' or (args.episode_controls and hil_policy)) and args.recording_mode != 'inference-only'
    run = (runtime/'sessions' if start_bounded else episode_root)/run_id
    shadow_workspace = workspace(runtime) if shadow_run else None
    if shadow_workspace is not None:
        run = Path(shadow_workspace.name)/run_id
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
        if config.get('manual_compute') == 'process_v1':
            from .manual_compute import ManualPlanner
            import copy
            gate_spec = copy.deepcopy(gate.plan_worker_spec)
            gate_spec['runtime'] = str(run/'manual_worker')
            manual_planner = ManualPlanner(dict(gate=gate_spec, urdf=str(urdf), ik=config['ik'],
                                                state=owner.last_submitted.tolist()),
                                          max_age_s=owner.output.timeout)
        policy = None
        if args.mode in ('run','policy-shadow','sync','sync-shadow'):
            config['policy']['require_capabilities'] = True
            if config['policy'].get('transport') == 'eef_websocket_v2':
                from .websocket_policy import WebSocketPolicy
                from .rtc_policy import RTCPolicy
                config['policy']['shadow_only'] = not powered
                policy_class = RTCPolicy if args.rtc or hil_policy else WebSocketPolicy
                policy = policy_class(config['policy'], args.task, mapping.check_action,
                                         held_getter=lambda: owner.last_submitted.copy())
            else:
                if hil_policy:
                    raise ValueError('RTC HIL requires the bound EEF RTC WebSocket transport')
                policy = HttpPolicy(config['policy'], args.task, mapping.check_action)
            policy.health()
            if args.rlt_hil:
                from .rlt_policy import RLTPolicy
                rlt_config = dict(config['rlt_policy'], require_capabilities=True, shadow_only=not powered)
                if config.get('evaluation'):
                    rlt_config['allow_boundary_updates'] = False
                rlt_policy = RLTPolicy(rlt_config, args.task, mapping.check_action,
                                       held_getter=lambda: owner.last_submitted.copy())
                rlt_policy.health()
                if rlt_policy.rlt_identity['base_model_version'] != policy.model_version:
                    raise ValueError('RLT parent differs from bound B RTC model')
                policy.prepare()
                rlt_policy.prepare()
                print('F1 RLT：B／actor通信连接已准备；等待真实START，不发送推理或动作。', flush=True)
        evaluation = None
        if config.get('evaluation'):
            if not args.rlt_hil:
                raise ValueError('Local evaluation requires the RLT HIL recording entry')
            from .evaluation import validate_evaluation
            if config['evaluation'].get('actor_identity'):
                rlt_policy.pin_evaluation(config['evaluation']['actor_identity'])
            evaluation = validate_evaluation(config['evaluation'], policy, rlt_policy)
        binding = dict(runtime_identity=identity, config_sha256=config['_config_sha256'], profile_sha256=config['_profile_sha256'],
            infra_manifest_sha256=config['_infra_manifest_sha256'], model_id=None if policy is None else policy.model_id,
            execution_mode='powered' if powered else 'shadow', robot='F1-09',
            ik_verified=config['ik']['verified'], recording_mode=args.recording_mode,
            capture_hz=config['capture_hz'], policy_frequency_hz=config['policy']['frequency_hz'],
            camera_identity_source=CAMERA_IDENTITY, purpose=purpose, auto_video=video_enabled,
            f710_home=profile.get('f710_home'),
            authorization='local_enter' if standalone else 'f710_start',
            standalone_sync=standalone and not args.rtc, standalone_rtc=args.rtc,
            rtc_hil=hil_policy, rlt_hil=args.rlt_hil,
            rtc_parameters=dict(delay_steps=args.rtc_delay_steps,min_steps=args.rtc_min_steps) if args.rtc or hil_policy else None,
            duration_seconds=(None if args.continuous else (args.duration_seconds or 60)) if standalone or hil_policy else None,
            session_config=session, task=args.task, model_version=None if policy is None else policy.model_version)
        if rlt_policy is not None:
            binding.update(collection_purpose='evaluation' if evaluation else 'training', evaluation=evaluation)
            binding.update(base_model_id=policy.model_id, base_model_version=policy.model_version,
                           model_id=rlt_policy.model_id, model_version=rlt_policy.model_version)
            binding['rlt'] = dict(rlt_policy.rlt_identity, model_id=rlt_policy.model_id,
                                  model_version=rlt_policy.model_version, phase_control='f1_rlt_local_v4',
                                  base_execution='rtc', actor_execution='sync_c16_hold_then_execute')
        if config.get('hardware_capabilities') is not None:
            binding['hardware_capabilities'] = config['hardware_capabilities']
            binding['state_valid_mask'] = list(source.state_valid_mask)
        if config.get('fault_lifecycle'):
            binding['fault_lifecycle'] = config['fault_lifecycle']
        if start_bounded:
            from .demonstration import DemonstrationRecorder
            binding.update(recording_boundary='f1_rlt_local_v4' if args.rlt_hil else 'f710_start_up_v2' if args.episode_controls else 'f710_start_v1', process_run_id=run_id,
                           process_diagnostics_path=str(run))
            from .recording_audio import RecordingAudio, POLICY_CUE_LEVELS
            episodes = DemonstrationRecorder(episode_root, runtime, binding, args.task,
                audio=RecordingAudio(prompt=args.rlt_hil, cue_levels=POLICY_CUE_LEVELS if args.rlt_hil else None) if powered and args.episode_controls else RecordingAudio(runtime/'recording_audio.json') if powered else None,
                unified_buttons=args.episode_controls)
            recorder = sink = episodes
        else:
            recorder = ShadowRecorder() if shadow_run else F1Recorder(run/'journal', episode_id=run_id, task_prompt=args.task, binding=binding)
            sink = make_sink(run/'payload', args.recording_mode)
        if (hil_policy or args.episode_controls) and powered:
            from .rtc_hil_feedback import RTCHILFeedback
            hil_feedback = RTCHILFeedback(audio=episodes.audio if args.rlt_hil and episodes else None,
                                           episode_mode=args.episode_controls, rlt_mode=args.rlt_hil)
        def emit(event, **fields):
            now = fields.pop('monotonic_ns', time.monotonic_ns())
            if not getattr(engine, 'recording_failed', None):
                if not recorder.record_event(event, monotonic_ns=now, **fields):
                    from .faults import RecordingUnavailable
                    raise RecordingUnavailable('event journal overflow')
            if hil_feedback:
                hil_feedback.notify(event, fields)
        if powered and not args.rlt_hil:
            input('F1 sync: confirm scene and physical emergency stop ready; press Enter to begin. ' if standalone else
                  'F1 upper16: local operator ready. Press Enter, then use F710 START to begin. ')
        engine_class = StandalonePolicyEngine if standalone else F1PolicyEngine
        engine_options = dict(duration_seconds=binding['duration_seconds'], stop_requested=stop.is_set) if standalone or hil_policy else {}
        if args.rtc:
            from .rtc import RTCPolicyEngine
            engine_class = RTCPolicyEngine
            engine_options.update(rtc_delay_steps=args.rtc_delay_steps, rtc_min_steps=args.rtc_min_steps)
        if hil_policy:
            from .rtc_hil import RTCHILPolicyEngine
            engine_class = RTCHILPolicyEngine
            engine_options.update(rtc_delay_steps=args.rtc_delay_steps, rtc_min_steps=args.rtc_min_steps)
        if args.rlt_hil:
            from .rlt_hil import RLTHILPolicyEngine
            engine_class = RLTHILPolicyEngine
            engine_options['rlt_policy'] = rlt_policy
            engine_options['evaluation'] = evaluation
        engine = engine_class(emit, source, policy, recorder, sink, config,
            teleop_only=args.mode not in ('run','policy-shadow','sync','sync-shadow'), episodes=episodes, **engine_options)
        if args.rlt_hil:
            print('RLT-HIL：START 开条运行 B1250／人工接管／交回 VLA；→每条进入一次局部 RLT；↑成功／↓失败；未评分不能交还或Y；人工接管始终可用；Y复位、稳定两秒后封存。', flush=True)
        if engine.fault_lifecycle:
            from .runtime import SupervisedControlLoop
            loop = SupervisedControlLoop(engine, owner, arm, recorder, profile, manual_planner=manual_planner)
        else:
            loop = ControlLoop(engine, owner, arm, recorder, profile)
        execution_mode = 'powered' if powered else 'shadow'
        if not standalone:
            server = IntentServer(args.port, execution_mode=execution_mode, identity=ack_identity(identity) + (';rlt_gamepad_v1=1;rlt_local_v4=1' if args.rlt_hil else ''))
            engine.recovery_proof = server.recovery.age
        from .status import StatusPublisher
        status_publisher = StatusPublisher(runtime/'status.json', engine, sink, binding, run_id, finish_event=stop)
        def request_stop(signum, _frame):
            termination.update(reason='signal', signal=signal.Signals(signum).name)
            stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, request_stop)
        if standalone:
            engine.authorize_local(time.monotonic_ns())
            resume_input = LocalResumeInput()
            print(f'F1 {execution_mode.upper()} standalone sync: duration={binding["duration_seconds"]}; Ctrl+C stops; {"recording=off" if shadow_run else "raw="+str(run)}', flush=True)
        else:
            print(f'F1 {execution_mode.upper()} ({args.mode}): 127.0.0.1:{args.port}; waiting for START; {"recording=off" if shadow_run else "raw="+str(run)}', flush=True)
        print('F1 SHADOW: no command publishers.' if not powered else
              'F1 POWERED: local authorization and all runtime gates required.', flush=True)
        if episodes:
            print('RLT：START开条／人工接管／交回VLA；→每条进入一次放置；↑成功／↓失败；未评分不能交还或Y；人工接管始终可用；Y复位、稳定两秒后封存。' if args.rlt_hil else '连续录制：START开始／接管；松RB并等夹爪完成后↑保存；Y可选回位。Ctrl+C退出。', flush=True)
        while not stop.is_set():
            started = time.monotonic_ns()
            incoming, reason = (None, None) if standalone else server.snapshot(receiver_status(engine))
            loop.step(started, incoming, reason)
            if standalone:
                resume_input.poll(engine, time.monotonic_ns())
            status_publisher.update(incoming, getattr(arm,'last_target_limit_reason',None))
            stop.wait(max(0., .004-(time.monotonic_ns()-started)/1e9))
    except StandaloneFinished as exc:
        termination['reason'] = str(exc) if termination['signal'] is None else 'signal'
        emit('standalone_finished', reason=str(exc))
        print('F1 sync stopped: '+str(exc), flush=True)
    except KeyboardInterrupt:
        termination.update(reason='keyboard_interrupt', signal='SIGINT')
    except BaseException as exc:
        termination.update(reason='exception', error=type(exc).__name__ + ': ' + str(exc)[:1000])
        status = 'failed'
        print(f'F1 FAILED: receiver is stopping; {"recording=off" if shadow_run else "raw="+str(run)}; failure={run / "failure.txt"}',
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
            if not episodes and not shadow_run:
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
        if manual_planner:
            cleanup(manual_planner.close)
        if server:
            cleanup(server.close)
        if source:
            cleanup(source.stop_capture)
        if engine:
            cleanup(engine.close)
        elif source:
            source.stop_event.set()
        if 'policy' in locals() and hasattr(policy, 'close'):
            cleanup(policy.close)
        if rlt_policy:
            cleanup(rlt_policy.close)
        if transport:
            cleanup(transport.close)
        if control_lock:
            cleanup(control_lock.close)
        if source and run.is_dir():
            cleanup(lambda: (run/'camera_diagnostics.json').write_text(json.dumps(source.diagnostics(), indent=2)))
        if errors:
            status = 'failed'
        if sink and not cleanup(lambda: sink.close(status=status, termination=termination)
                                if sink is episodes else sink.close(status=status)):
            status = 'failed'
        if (standalone or hil_policy) and recorder:
            cleanup(lambda: recorder.record_event('session_termination', monotonic_ns=time.monotonic_ns(),
                status=status, cleanup_errors=list(errors), **termination))
        if recorder and recorder is not episodes and not cleanup(lambda: recorder.close(status=status)):
            errors.append('journal not published as complete')
        if (standalone or hil_policy) and recorder and not episodes and not shadow_run:
            try:
                from .telemetry import summarize_session
                summarize_session(run, termination=termination)
            except Exception as exc:
                print('F1 TELEMETRY summary failed: '+str(exc), file=sys.stderr, flush=True)
        if status == 'completed' and not errors and recorder and not episodes and not shadow_run:
            # Queue creation is after commit and outside the control lifecycle.
            try:
                from .interventions import enqueue_session
                enqueue_session(run, runtime/'video-jobs')
            except Exception as exc:
                print('F1 VIDEO queue pending recovery: '+str(exc), file=sys.stderr)
        try:
            if not episodes and not shadow_run:
                result = publish_completion(runtime, run, binding, 'saved' if status == 'completed' and not errors and recorder else 'failed', errors)
                print('F1 SESSION '+json.dumps(result, ensure_ascii=False), flush=True)
                if hil_feedback and result['phase'] == 'saved':
                    hil_feedback.notify('session_saved')
        except OSError as exc:
            errors.append('completion status: '+str(exc))
        if hil_feedback:
            hil_feedback.close()
        if shadow_workspace is not None:
            cleanup(shadow_workspace.cleanup)
            summary = recorder.summary() if isinstance(recorder, ShadowRecorder) else dict(recording=False)
            print('F1 SHADOW END '+json.dumps(dict(summary, status='failed' if errors else status,
                termination=termination, errors=list(errors)), ensure_ascii=False), flush=True)
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        if errors:
            print('F1 cleanup incomplete: ' + '; '.join(errors) + f'; {"recording=off" if shadow_run else "raw="+str(run)}',
                  file=sys.stderr, flush=True)
    return 1 if status == 'failed' or errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
