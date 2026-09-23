"""One Robot owner: POLICY/fresh-policy HOLD use canonical single-34D normal Servo.

Human intervention retains arms14 Servo plus explicit O10 point commands.
"""
import threading
import time
import numpy as np


class RecordedRobot:
    def __init__(self, robot, recorder):
        self.robot = robot
        self.recorder = recorder
        self.lock = threading.RLock()
        self.calls = []
        self.joint_indices = {}

    def __getattr__(self, name):
        value = getattr(self.robot, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            with self.lock:
                started = time.monotonic_ns()
                is_command = name in {"joint_servo_control", "move_ee_pos"}
                row = dict(api=name, interface=name, started_monotonic_ns=started)
                if is_command and args and name == "joint_servo_control":
                    row.update(joint_names=list(args[0].joint_names),
                               joint_positions=list(args[0].joint_positions),
                               enable_low_latency=kwargs.get("enable_low_latency", False))
                    row['joint_indices'] = [self.joint_indices.get(n.strip()) for n in args[0].joint_names]
                if name == "move_ee_pos" and args and hasattr(args[0], "states"):
                    row.update(group=args[0].group, target_type=args[0].target_type,
                               joint_positions=[float(s.position) for s in args[0].states])
                    row['joint_indices'] = list(range(14, 34)) if args[0].group == 'dual_tool' else []
                if 'joint_positions' in row:
                    row['submitted_positions'] = row['joint_positions'].copy()
                try:
                    result = value(*args, **kwargs)
                except Exception as exc:
                    row.update(error=f"{type(exc).__name__}: {exc}", sdk_return=None)
                    raise
                else:
                    if is_command:
                        row["sdk_return"] = result if isinstance(result, (int, float, str, bool)) else str(result)
                    return result
                finally:
                    if is_command:
                        row["completed_monotonic_ns"] = time.monotonic_ns()
                        row['finished_monotonic_ns'] = row['completed_monotonic_ns']
                        self.calls.append(row)
                        self.recorder.record_event("actual_sdk_call",
                            monotonic_ns=row["completed_monotonic_ns"], **row)
        return call

    def drain_calls(self):
        calls, self.calls = self.calls, []
        return calls


def controller_type(base):
    class HILController(base):
        def send_action(self, action):
            # Serialize the complete dispatch with observation state reads;
            # actual SDK names/values/return remain recorded by the proxy.
            with self._robot.lock:
                return super().send_action(action)

        def prepare_hil_layouts(self):
            if self._servo_output_mode != "arms-only" or self._hand_thread is not None:
                raise RuntimeError("HIL requires sole arms-only controller without hand worker")
            self._human_max_send_seconds = self._max_send_seconds
            # Validate the live names and retain canonical recording indices.
            # These side layouts are validation metadata, not POLICY dispatch.
            self._dual_side_servo_layouts = self._read_dual_side_servo_layouts()
            self._robot.joint_indices = {
                name.strip(): index for names, indices in self._dual_side_servo_layouts
                for name, index in zip(names, indices, strict=True)}
            if set(self._robot.joint_indices.values()) != set(range(34)):
                raise RuntimeError('dynamic layouts do not cover the 34D recording contract')

        def _record_policy_timing_warning(self, event, duration, threshold):
            self._robot.recorder.record_event(event, monotonic_ns=time.monotonic_ns(),
                request_layout="unified-34", duration_seconds=duration,
                threshold_seconds=threshold, action="continue")

        def set_hil_execution(self, human):
            # Caller is the one dispatch thread. No new controller, Servo owner,
            # hand worker, low-latency mode, or simultaneous output path.
            self._servo_output_mode = "arms-only" if human else "unified-34"
            self._max_send_seconds = self._human_max_send_seconds if human else 0.2
            self._timing_warning_callback = None if human else self._record_policy_timing_warning
    return HILController


def record_dispatch(engine, proxy, *, now, sequence, hold, selected, submitted,
                    source, human_arm=False, human_hand=False):
    hil = engine.supervisor
    if source.startswith('human_') and source != 'human_hold':
        source = ('human_arm_and_hand' if human_arm and human_hand else
                  'human_arm' if human_arm else 'human_hand' if human_hand else 'human_hold')
    if source == 'home':
        human_arm = human_hand = False  # Automated reset is not expert correction.
    if source == 'hold' and hil.state.value == 'policy':
        source = 'policy_wait'
    if not engine.recorder.record_control_tick(monotonic_ns=now, control_sequence=sequence,
            hil_state=hil.state.value, control_source=source, intervention_id=hil.intervention_id,
            policy_epoch=hil.policy_epoch,
            policy_request_id=engine.chunk_request_id if source == "policy" else None,
            policy_action_index=engine.action_index if source == "policy" else None,
            intervention_mask=hil.state.value == "hil_latched",
            human_arm_mask=human_arm, human_hand_mask=human_hand,
            hold_34d=hold,
            policy_proposal_34d=selected if source == "policy" else None,
            human_candidate_34d=selected if source.startswith("human") or source == "home" else None,
            selected_command_34d=selected, submitted_command_34d=submitted,
            executed_command_34d=None, measured_34d=None,
            sdk_calls=proxy.drain_calls(), evidence_kind="real_sdk_submission_not_measured_execution",
            policy_sampling=engine.policy_sampling if source == 'policy' else None):
        raise RuntimeError("control journal rejected record")


def progressive_hand_pending(scheduler, packet):
    """Command-generation convergence, not a certificate of physical completion."""
    if packet.active_side not in {"left", "right"}:
        return False
    if packet.grip_target is None:
        return bool(packet.grip_velocity or packet.thumb_clearance)
    side = packet.active_side
    target = scheduler.state.synergy.target(side, packet.grip_target, 0.)
    indices = slice(0, 10) if side == "left" else slice(10, 20)
    # Numerical equality of the generated command, not an experimental motion threshold.
    return not np.allclose(scheduler.state.command[indices], target, rtol=0., atol=1e-12)
