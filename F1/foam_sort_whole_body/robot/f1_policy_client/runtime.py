"""Synchronous H20 execution using the established F1 replay controller."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import signal
import time
import uuid

from f1_replay.bundle import ReplayFrame
from f1_replay.cli import (Journal, Recorder, check_execution_host,
                           select_recording_topics, wait_for_enter)
from f1_replay.runtime import (ReplayError, ReplayInterrupted, base_stopped,
                               check_command_activity, check_command_peers,
                               check_controller_parameters, check_feedback,
                               run_schedule, stop_motion)
from .observation import CameraObservationError, capture_observation
from .execution import FirstArmStepRejected, ObservationExpired, prepare_prefix
from .policy import HttpPolicy
from .ros import RosObservationBackend
from .body_reference import check_machine


def wait_request(future, tick, timeout_s, *, now=time.monotonic, sleep=time.sleep):
    """Only HTTP runs in the worker; ROS remains on its owning main thread."""
    deadline = now() + timeout_s
    while not future.done():
        tick()
        if now() >= deadline:
            raise ReplayError("推理请求超时；不重发或执行迟到结果")
        sleep(0.005)
    tick()
    return future.result()


def wait_base_settled(backend, profile, tick, log, *, now=time.monotonic_ns,
                      sleep=time.sleep, stable_s=.25):
    """Confirm sustained new feedback before observing/requesting the next chunk.

    A single zero-speed sample from stop_motion is insufficient: the next ROS
    sample can still report deceleration. Keep zero commands within the existing
    stop timeout; never dispatch a policy request while waiting.
    """
    started = now()
    deadline = started + round(profile['stop_timeout_s'] * 1e9)
    stable_since = None
    last_received = started
    samples = 0
    last_base = {}
    while now() < deadline:
        tick()  # Cancellation, ownership, feedback freshness and recorder health.
        sent = backend.stop_base()
        if sent is None:
            raise ReplayError('段间停车零速命令未发布')
        log('inter_chunk_zero', sent)
        sleep(min(1. / profile['stop_publish_hz'], max(0., (deadline-now())/1e9)))
        tick()
        if now() >= deadline:
            break
        snapshot = backend.snapshot()
        last_base = dict(snapshot.get('base', {}))
        received = last_base.get('received_ns')
        stopped = base_stopped(snapshot, profile, now())
        new_sample = type(received) is int and received > last_received
        if not stopped:
            stable_since = None
        if new_sample:
            if received-last_received > profile['feedback_max_age_s'] * 1e9:
                stable_since = None
            last_received = received
            samples += 1
            if stopped and stable_since is None:
                stable_since = received
        log('inter_chunk_base_feedback', {'base': last_base, 'stopped': stopped,
            'new_sample': new_sample, 'elapsed_s': (now()-started)/1e9})
        if (new_sample and stopped and stable_since is not None
                and received-stable_since >= stable_s * 1e9):
            result = {'stable_s': (received-stable_since)/1e9,
                      'elapsed_s': (now()-started)/1e9, 'new_samples': samples,
                      'base': last_base, 'zero_velocity_feedback_confirmed': True}
            log('inter_chunk_base_settled', result)
            return result
    raise ReplayError(f'段间停车等待超时（{profile["stop_timeout_s"]}秒），未取得连续{stable_s}秒新鲜静止反馈；最后反馈={last_base}')


def observe(backend, cfg, profile):
    backend.poll(0.0)
    return capture_observation(backend.snapshot(), now_ns=time.monotonic_ns(),
                               feedback_max_age_s=profile["feedback_max_age_s"],
                               camera_max_age_s=cfg["camera_max_age_s"],
                               camera_max_spread_s=cfg["camera_max_spread_s"])


def wait_observation(backend, cfg, profile, stopped=lambda: False):
    deadline = time.monotonic() + cfg["feedback_wait_s"]
    error = ReplayError("未取得观测")
    while time.monotonic() < deadline:
        if stopped():
            raise ReplayInterrupted("操作者停止")
        backend.poll(0.05)
        try:
            observation = observe(backend, cfg, profile)
            check_command_peers(backend, profile)
            return observation
        except (ReplayError, ValueError) as exc:
            error = exc
    raise ReplayError(f"观测/控制接口未就绪：{error}")


def wait_camera_observation(backend, cfg, profile, tick, log, *,
                            now=time.monotonic, sleep=time.sleep):
    """Wait for a fresh camera tuple; feedback/ownership failures remain fatal."""
    started = now()
    deadline = started + cfg.get("camera_wait_s", 1.0)
    retries = 0
    while True:
        tick()
        try:
            observation = observe(backend, cfg, profile)
        except CameraObservationError as exc:
            retries += 1
            detail = {"reason": str(exc), "cameras": exc.diagnostics,
                      "wait_s": now()-started, "retries": retries}
            if retries == 1:
                log("camera_wait", detail)
            if now() >= deadline:
                log("camera_wait_timeout", detail)
                raise ReplayError(f"等待新鲜三图超时：{exc}") from exc
            sleep(0.005)
            continue
        if retries:
            log("camera_wait_recovered", {"wait_s": now()-started, "retries": retries,
                                          "cameras": observation.camera_metadata})
        return observation


def reference_frame(observation, profile):
    # Existing check_feedback expects the replay state21, not policy state19.
    return ReplayFrame(0, observation.captured_ns,
                       tuple(observation.state19[:16]) + tuple(profile["inference_body_reference"]["body_positions_deg"]),
                       tuple(observation.state19))


def check_selected_checkpoint(doc, health):
    """A switched/mis-forwarded endpoint must not change the selected A/B model."""
    expected = doc.get("checkpoint")
    if expected:
        actual = health.get("model_binding", {}).get("checkpoint_identity", {}).get("checkpoint")
        if not isinstance(actual, str) or Path(actual) != Path(expected):
            raise ReplayError(f"推理服务checkpoint与本入口不符：期望{expected}，实际{actual}")
    expected_prefix=doc.get('client',{}).get('profile_overrides',{}).get('inference_prefix_policy','fixed')
    if health.get('prefix_policy','fixed')!=expected_prefix:
        raise ReplayError('推理服务可执行前缀合同与本入口不符')


def preflight(doc, profile):
    check_machine(profile)
    cfg = doc["client"]
    policy = HttpPolicy(cfg["endpoint"], cfg["request_timeout_s"])
    health = policy.health()
    check_selected_checkpoint(doc, health)
    backend = RosObservationBackend(profile, cfg["camera_topics"])
    try:
        backend.open()
        observation = wait_observation(backend, cfg, profile)
        parameters = backend.read_controller_parameters(timeout_sec=5)
        check_controller_parameters(parameters["parameters"], profile)
        base_parameters = (backend.verify_base_parameters()
                           if profile.get("base_controller_expected_parameters") else None)
        observation = wait_observation(backend, cfg, profile)
        check_feedback(backend.snapshot(), reference_frame(observation, profile), profile,
                       time.monotonic_ns(), initial=True)
        recording_types = None
        if profile.get('check_recording_types'):
            from f1_replay.recording_check import check_types
            recording_types = check_types(profile, backend)
        return {"preflight_passed": True, "hardware_published": False,
                "control_publishers_created": False, "health": health,
                "state19": list(observation.state19), "body5": list(observation.body5),
                "camera_metadata": observation.camera_metadata,
                "controller_parameters": parameters, "base_parameters": base_parameters,
                "recording_type_check": recording_types,
                "note": "接口就绪不代表所选模型动作或闭环成功"}
    finally:
        backend.close()


def run(doc, profile, output, *, powered=False, chunks=1):
    """Never dispatch rejected prefixes; optionally reobserve bounded first-step failures."""
    check_machine(profile)
    if powered:
        check_execution_host(profile)
    if type(chunks) is not int or chunks < 0:
        raise ReplayError("chunks必须为非负整数；0表示持续到现场停止")
    cfg = doc["client"]
    output = Path(output)
    policy = HttpPolicy(cfg["endpoint"], cfg["request_timeout_s"])
    backend = RosObservationBackend(profile, cfg["camera_topics"])
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="f1-policy-http")
    log = Journal(output / "events.jsonl")
    recorder = None
    outputs_possible = False
    stop_requested = False
    old_handlers = {}
    body_frame = None
    last_peers = 0
    consecutive_expired = 0
    consecutive_step_retries = 0
    result = {"mode": "execute" if powered else "shadow", "chunks_requested": 0,
              "chunks_accepted": 0, "chunks_rejected": 0, "chunks_executed": 0,
              "rejection_reasons": {}, "hardware_published": False,
              "expired_responses": 0,
              "first_arm_retries": 0,
              "first_arm_policy": profile.get("inference_first_arm_policy", "reject"),
              "inter_chunk_policy": cfg["inter_chunk_policy"],
              "onsite_task_success": "not_assessed"}

    def cleanup_log(event, fields):
        # A failed disk must not interrupt the established stop/hold sequence.
        try:
            log(event, fields)
        except Exception as exc:
            errors = result.setdefault("logging_errors", [])
            if len(errors) < 10:
                errors.append(f"{event}: {exc}")

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    def tick():
        nonlocal last_peers
        if stop_requested:
            raise ReplayInterrupted("操作者停止推理")
        backend.poll(0.0)
        check_command_activity(backend, profile)
        if body_frame is not None:
            check_feedback(backend.snapshot(), body_frame, profile, time.monotonic_ns())
        if recorder:
            recorder.check()
        if time.monotonic() - last_peers >= 1:
            check_command_peers(backend, profile, enabled=outputs_possible)
            last_peers = time.monotonic()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, request_stop)
        health = policy.health()
        check_selected_checkpoint(doc, health)
        if (cfg['inter_chunk_policy'] in ('async_native_h50_v1', 'sync_raw_h50_v1')
                and health.get('execution_mode') != 'async_native_h50_v1'):
            raise ReplayError('所选服务不支持H50，尚未创建机器人控制端点')
        binding = health["model_binding"]
        log("run_start", {"config": doc, "profile": profile, "health": health,
                          "mode": result["mode"]})
        backend.open()
        observation = wait_observation(backend, cfg, profile, lambda: stop_requested)
        body_frame = reference_frame(observation, profile)
        parameters = backend.read_controller_parameters(timeout_sec=5)
        check_controller_parameters(parameters["parameters"], profile)
        if profile.get("base_controller_expected_parameters"):
            log("base_parameters", backend.verify_base_parameters())
        log("controller_parameters", parameters)
        tick()
        if powered:
            check_feedback(backend.snapshot(), body_frame, profile, time.monotonic_ns(), initial=True)
            if profile.get('check_recording_types'):
                from f1_replay.recording_check import check_types
                log('recording_type_check', check_types(profile, backend))
            print("连续H50：边执行边请求，按时间跳过旧点，不在段间停车。" if cfg["inter_chunk_policy"] == "async_native_h50_v1" else "原始50/50：完整执行50点，再取图请求；空档底盘零速，不等待停稳。" if cfg["inter_chunk_policy"] == "sync_raw_h50_v1" else "20/50：执行前20点后重新取图；空档底盘零速，不等待停稳。" if cfg["inter_chunk_policy"] == "sync_raw_h20_v1" else "将执行模型双臂、夹爪和底盘输出；每20步停车后重新观测。")
            if profile.get('inference_first_arm_policy') == 'report_only':
                print('模型测试：首步关节差只记录、不拒绝；模型IK轨迹不插补、不改写。物理限位、反馈与急停检查保留。')
            wait_for_enter(lambda: stop_requested)
            tick()
            topics, diagnostic = select_recording_topics(profile, backend)
            log("recording_diagnostics", diagnostic)
            recorder = Recorder(output, topics)
            # Match replay startup: rosbag cannot discover command topics until
            # publishers exist. Creation sends no messages; the first policy
            # dispatch remains after recorder readiness and complete validation.
            outputs_possible = True
            backend.enable_commands()
            log("recording_start", {"command": recorder.start(tick, backend)})
            tick()
        if cfg['inter_chunk_policy'] in ('async_native_h50_v1', 'sync_raw_h50_v1', 'sync_raw_h20_v1'):
            from .native import run_loop
            result['_native_binding'] = binding
            result['_native_capability'] = health.get('execution_mode')
            def native_publish(action):
                if stop_requested:
                    raise ReplayInterrupted('操作者停止推理')
                result['dispatch_attempted'] = True
                result['hardware_published'] = True
                return backend.publish(action)
            run_loop(doc, profile, backend, policy, pool, output, log, tick,
                     native_publish, result, powered=powered, chunks=chunks)
        else:
            # Expired requests do not consume the user's requested fresh chunk count.
            while chunks == 0 or (result["chunks_requested"] - result["expired_responses"]
                                  - result["first_arm_retries"]) < chunks:
                tick()
                if powered:
                    wait_base_settled(backend, profile, tick, log)
                observation = wait_camera_observation(backend, cfg, profile, tick, log)
                request_id = uuid.uuid4().hex
                request_dir = output / request_id
                request_dir.mkdir()
                images = {}
                for name, data in observation.images.items():
                    (request_dir / (name + ".image")).write_bytes(data)
                    images[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                log("observation", {"request_id": request_id, "observation_monotonic_ns": observation.captured_ns,
                                    "state19": list(observation.state19), "body5": list(observation.body5),
                                    "cameras": observation.camera_metadata, "images": images})
                result["chunks_requested"] += 1
                submitted_ns = time.monotonic_ns()
                future = pool.submit(policy.infer, observation, request_id)
                response = wait_request(future, tick, cfg["request_timeout_s"])
                received_ns = time.monotonic_ns()
                (request_dir / "response.json").write_text(json.dumps(response, indent=2, allow_nan=False) + "\n")
                log("request_timing", {"request_id": request_id,
                    "observation_to_submit_s": (submitted_ns-observation.captured_ns)/1e9,
                    "http_wait_s": (received_ns-submitted_ns)/1e9,
                    "response_write_s": (time.monotonic_ns()-received_ns)/1e9,
                    "server_timing_s": response.get("timing_seconds", {})})
                try:
                    prepared = prepare_prefix(response, observation, backend.snapshot(), profile,
                                              now_ns=time.monotonic_ns(), request_id=request_id,
                                              expected_model_binding=binding,
                                              max_observation_age_s=cfg["max_observation_age_s"])
                except (ReplayError, ValueError) as exc:
                    reason = str(exc)
                    result["chunks_rejected"] += 1
                    reasons = result["rejection_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                    log("chunk_rejected", {"request_id": request_id, "reason": reason})
                    if isinstance(exc, ObservationExpired):
                        result["expired_responses"] += 1
                        consecutive_expired += 1
                        # This runtime requests only after confirmed stop/hold.
                        # Continue polling and get a NEW observation on the next loop.
                        if consecutive_expired >= cfg.get("max_consecutive_expired", 3):
                            raise ReplayError(f"连续{consecutive_expired}次观测过期：{exc}") from exc
                        log("expired_response_retry", {"request_id": request_id,
                            "consecutive_expired": consecutive_expired,
                            "policy": "discard_response_and_capture_new_observation"})
                        continue
                    consecutive_expired = 0
                    if (powered and isinstance(exc, FirstArmStepRejected)
                            and consecutive_step_retries < cfg.get("first_arm_retry_limit", 0)):
                        # Requesting occurs only while the base is stopped. Explicitly
                        # re-confirm stop/hold, discard this prefix and take new input.
                        stopped = stop_motion(backend, profile, log)
                        if not (stopped["zero_velocity_feedback_confirmed"]
                                and stopped["fresh_arm_hold_published"]):
                            raise ReplayError("首步重采前停车/保持未确认") from exc
                        consecutive_step_retries += 1
                        result["first_arm_retries"] += 1
                        log("first_arm_retry", {"request_id": request_id,
                            "attempt": consecutive_step_retries,
                            "limit": cfg["first_arm_retry_limit"], "reason": reason,
                            "policy": "discard_prefix_stop_hold_capture_new_observation"})
                        continue
                    if powered:
                        raise
                    continue
                consecutive_expired = 0
                consecutive_step_retries = 0
                result["chunks_accepted"] += 1
                log("chunk_accepted", {"request_id": request_id,
                                       "gripper_mapping": list(prepared.gripper_mapping),
                                       "first_arm_delta_deg": list(prepared.first_arm_delta_deg),
                                       "commands19": [list(f.action) for f in prepared.bundle.frames]})
                if powered:
                    if time.monotonic_ns() - observation.captured_ns > cfg["max_observation_age_s"] * 1e9:
                        raise ReplayError("执行前观测已过期")
                    def publish(action):
                        if stop_requested:
                            raise ReplayInterrupted("操作者停止推理")
                        result["dispatch_attempted"] = True
                        try:
                            sent = backend.publish(action)
                        except Exception as exc:
                            partial = getattr(exc, "record", None)
                            result["hardware_published"] = True  # Conservative: partial sends may have occurred.
                            cleanup_log("publication_failed", {"record": partial,
                                                                "last_publication": backend.last_publication})
                            raise
                        result["hardware_published"] = sent is not None
                        return sent
                    run_schedule(prepared.bundle, publish, tick, log, profile["max_lateness_s"],
                                 max_command_hz=profile["max_command_hz"], stopped=lambda: stop_requested)
                    result["chunks_executed"] += 1
                    stopped = stop_motion(backend, profile, log)
                    if not stopped["zero_velocity_feedback_confirmed"] or not stopped["fresh_arm_hold_published"]:
                        raise ReplayError("块末停车/双臂保持未确认，不能继续请求")
                    log("inter_chunk_stop", stopped)
        result["completed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        cleanup_log("run_error", {"error": result["error"]})
    finally:
        # Stop before waiting for network worker or recorder teardown.
        if outputs_possible:
            result["stop_commands_attempted"] = True
            result["hardware_published"] = True  # Stop/hold are also control commands.
            try:
                result["stop"] = stop_motion(backend, profile, cleanup_log)
                if not (result["stop"]["zero_velocity_feedback_confirmed"]
                        and result["stop"]["fresh_arm_hold_published"]):
                    result["error"] = result.get("error", "退出停车/保持未确认，请现场检查")
            except Exception as exc:
                result["error"] = result.get("error", f"停车失败：{exc}")
        if recorder:
            try:
                result["recording"] = recorder.close()
            except Exception as exc:
                result["error"] = result.get("error", f"录包关闭失败：{exc}")
        try:
            backend.close()
        except Exception as exc:
            result["error"] = result.get("error", f"ROS关闭失败：{exc}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            cleanup_log("run_end", result)
            try:
                log.close()
            except Exception as exc:
                result.setdefault("logging_errors", []).append(f"journal close: {exc}")
            try:
                (output / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
            except OSError as exc:
                result.setdefault("logging_errors", []).append(f"report write: {exc}")
    return result
