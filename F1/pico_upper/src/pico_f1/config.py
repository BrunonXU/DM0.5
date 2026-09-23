from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any


def project_root() -> Path:
    configured = os.environ.get("PICO_F1_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def arm_output_limits(config: dict[str, Any]) -> dict[str, float]:
    """Separate final tracking dynamics from unchanged upstream path limits.

    Missing overrides reproduce the previous smooth profile. The 2x ceiling
    is a software configuration guardrail, not a certified hardware rating.
    """
    overrides = config.get("arm_output", {})
    keys = {"max_acceleration_deg_s2", "max_jerk_deg_s3"}
    if not isinstance(overrides, dict) or set(overrides) - keys:
        raise ValueError("arm_output accepts only acceleration and jerk limits")
    result = {"max_velocity": float(config["max_velocity_deg_s"])}
    for name, key in (("max_acceleration", "max_acceleration_deg_s2"),
                      ("max_jerk", "max_jerk_deg_s3")):
        raw = overrides.get(key, config[key])
        if isinstance(raw, bool):
            raise ValueError(f"arm_output {key} must be a positive finite number")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"arm_output {key} must be a positive finite number") from exc
        if not math.isfinite(value) or not 0 < value <= 2 * float(config[key]):
            raise ValueError(f"arm_output {key} must be positive and <=2x upstream limit")
        result[name] = value
    return result


def load_control_config() -> dict[str, Any]:
    path = Path(
        os.environ.get(
            "PICO_F1_CONTROL_CONFIG",
            project_root() / "config" / "control.json",
        )
    )
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)

    required = {
        "command_hz",
        "collision_check_hz",
        "cartesian_scale",
        "max_velocity_deg_s",
        "max_acceleration_deg_s2",
        "max_jerk_deg_s3",
        "target_cutoff_hz",
        "joint_deadband_deg",
        "feedback_timeout_s",
    }
    missing = sorted(required.difference(config))
    if missing:
        raise ValueError(f"control config missing keys: {', '.join(missing)}")
    from pico_upper.contract import validate_profile
    validate_profile(config)
    arm_output_limits(config)
    from pico_f1.precision import precision_settings
    if 'precision' in config and config['precision'] is None:
        raise ValueError('precision must be an object')
    precision_settings(config.get('precision'))
    if not 10.0 <= float(config["command_hz"]) <= 100.0:
        raise ValueError("command_hz must be within [10, 100]")
    if float(config["collision_check_hz"]) != float(config["command_hz"]):
        raise ValueError("collision_check_hz must equal command_hz")
    if not 0.01 <= float(config["cartesian_scale"]) <= 1.5:
        raise ValueError("cartesian_scale must be within [0.01, 1.5]")
    chassis = config.get("chassis")
    if not isinstance(chassis, dict):
        raise ValueError("control config missing chassis object")
    chassis_required = {
        "enabled", "input_source", "body_follow_mode", "body_joint_index", "command_topic",
        "feedback_topic", "frame_id", "linear_position_scale",
        "yaw_position_scale", "position_gain_s_inv",
        "yaw_position_gain_s_inv", "linear_velocity_scale",
        "yaw_velocity_scale", "position_deadband_m",
        "yaw_position_deadband_rad", "min_linear_speed_m_s",
        "min_yaw_speed_rad_s", "immediate_response", "odometry_jump_m",
        "odometry_yaw_jump_rad", "max_forward_speed_m_s",
        "max_lateral_speed_m_s", "max_yaw_speed_rad_s",
        "linear_deadband_m_s", "yaw_deadband_rad_s", "input_cutoff_hz",
        "max_linear_acceleration_m_s2", "max_linear_jerk_m_s3",
        "max_yaw_acceleration_rad_s2", "max_yaw_jerk_rad_s3",
        "input_timeout_s", "feedback_timeout_s", "ab_chord_window_s",
        "joystick_enabled", "joystick_deadzone", "joystick_input_cutoff_hz",
        "joystick_input_timeout_s", "joystick_max_forward_speed_m_s",
        "joystick_max_lateral_speed_m_s", "joystick_max_yaw_speed_rad_s",
        "joystick_brake_linear_acceleration_m_s2", "joystick_brake_linear_jerk_m_s3",
        "joystick_brake_yaw_acceleration_rad_s2", "joystick_brake_yaw_jerk_rad_s3",
    }
    chassis_missing = sorted(chassis_required.difference(chassis))
    if chassis_missing:
        raise ValueError(f"chassis config missing keys: {', '.join(chassis_missing)}")
    if chassis["input_source"] not in {"auto", "body_pelvis", "headset"}:
        raise ValueError("chassis input_source must be auto, body_pelvis, or headset")
    if chassis["body_follow_mode"] not in {"velocity", "position"}:
        raise ValueError("chassis body_follow_mode must be velocity or position")
    if chassis.get("neutral_stop_mode", "profiled") not in {"profiled", "zero_target"}:
        raise ValueError("chassis neutral_stop_mode must be profiled or zero_target")
    if int(chassis["body_joint_index"]) != 0:
        raise ValueError("XR body pelvis joint index must be 0")
    for key in (
        "linear_position_scale", "yaw_position_scale", "position_gain_s_inv",
        "yaw_position_gain_s_inv", "linear_velocity_scale", "yaw_velocity_scale",
        "min_linear_speed_m_s", "min_yaw_speed_rad_s", "odometry_jump_m",
        "odometry_yaw_jump_rad", "max_forward_speed_m_s",
        "max_lateral_speed_m_s", "max_yaw_speed_rad_s", "input_cutoff_hz",
        "max_linear_acceleration_m_s2", "max_linear_jerk_m_s3",
        "max_yaw_acceleration_rad_s2", "max_yaw_jerk_rad_s3",
        "input_timeout_s", "feedback_timeout_s", "ab_chord_window_s",
        "joystick_input_cutoff_hz", "joystick_input_timeout_s",
        "joystick_max_forward_speed_m_s", "joystick_max_lateral_speed_m_s",
        "joystick_max_yaw_speed_rad_s",
        "joystick_brake_linear_acceleration_m_s2", "joystick_brake_linear_jerk_m_s3",
        "joystick_brake_yaw_acceleration_rad_s2", "joystick_brake_yaw_jerk_rad_s3",
    ):
        if not math.isfinite(float(chassis[key])) or float(chassis[key]) <= 0.0:
            raise ValueError(f"chassis {key} must be positive and finite")
    for key in (
        "position_deadband_m", "yaw_position_deadband_rad",
        "linear_deadband_m_s", "yaw_deadband_rad_s", "joystick_deadzone",
    ):
        if not math.isfinite(float(chassis[key])) or float(chassis[key]) < 0.0:
            raise ValueError(f"chassis {key} must be finite and non-negative")
    if not isinstance(chassis["immediate_response"], bool):
        raise ValueError("chassis immediate_response must be boolean")
    if not isinstance(chassis["joystick_enabled"], bool):
        raise ValueError("chassis joystick_enabled must be boolean")
    if not 0.0 <= float(chassis["joystick_deadzone"]) < 1.0:
        raise ValueError("chassis joystick_deadzone must be within [0, 1)")
    for axis in ("forward_speed_m_s", "lateral_speed_m_s", "yaw_speed_rad_s"):
        if float(chassis[f"max_{axis}"]) != float(chassis[f"joystick_max_{axis}"]):
            raise ValueError(f"body-follow and joystick chassis speed limits must match: {axis}")
    if 'braking_check' in chassis:
        from pico_f1.braking import yaw_braking_budget
        spec = chassis['braking_check']
        if not isinstance(spec, dict) or set(spec) != {
            'minimum_deceleration_rad_s2', 'response_allowance_s', 'max_overrun_deg'
        }:
            raise ValueError('invalid chassis braking_check fields')
        yaw_braking_budget(chassis, spec['minimum_deceleration_rad_s2'])

    torso = config.get("torso_pitch")
    if not isinstance(torso, dict):
        raise ValueError("control config missing torso_pitch object")
    torso_required = {
        "enabled", "joint_name", "joint_index", "command_topic", "frame_id",
        "a_hold_s", "pitch_scale", "min_position_deg", "max_position_deg",
        "max_relative_delta_deg", "max_input_step_deg", "input_timeout_s",
        "feedback_timeout_s", "max_velocity_deg_s", "max_acceleration_deg_s2",
        "max_jerk_deg_s3", "target_cutoff_hz", "joint_deadband_deg",
        "output_hz", "output_target_cutoff_hz", "output_deadband_deg",
    }
    torso_missing = sorted(torso_required.difference(torso))
    if torso_missing:
        raise ValueError(f"torso_pitch config missing keys: {', '.join(torso_missing)}")
    if not isinstance(torso["enabled"], bool):
        raise ValueError("torso_pitch enabled must be boolean")
    if torso["joint_name"] != "body_j3" or int(torso["joint_index"]) != 19:
        raise ValueError("torso_pitch must control body_j3 at full-body index 19")
    for key in (
        "a_hold_s", "max_relative_delta_deg", "max_input_step_deg",
        "input_timeout_s", "feedback_timeout_s", "max_velocity_deg_s",
        "max_acceleration_deg_s2", "max_jerk_deg_s3", "target_cutoff_hz",
        "output_hz", "output_target_cutoff_hz",
    ):
        if not math.isfinite(float(torso[key])) or float(torso[key]) <= 0.0:
            raise ValueError(f"torso_pitch {key} must be positive and finite")
    for key in ("pitch_scale", "min_position_deg", "max_position_deg"):
        if not math.isfinite(float(torso[key])):
            raise ValueError(f"torso_pitch {key} must be finite")
    if float(torso["pitch_scale"]) == 0.0:
        raise ValueError("torso_pitch pitch_scale must be non-zero")
    if float(torso["min_position_deg"]) >= float(torso["max_position_deg"]):
        raise ValueError("torso_pitch position limits are invalid")
    if (
        not math.isfinite(float(torso["joint_deadband_deg"]))
        or float(torso["joint_deadband_deg"]) < 0.0
    ):
        raise ValueError("torso_pitch joint_deadband_deg must be non-negative and finite")
    if (
        not math.isfinite(float(torso["output_deadband_deg"]))
        or float(torso["output_deadband_deg"]) < 0.0
    ):
        raise ValueError("torso_pitch output_deadband_deg must be non-negative and finite")
    if not 500.0 <= float(torso["output_hz"]) <= 1000.0:
        raise ValueError("torso_pitch output_hz must be within [500, 1000]")
    if float(torso["a_hold_s"]) <= float(chassis["ab_chord_window_s"]):
        raise ValueError("torso_pitch a_hold_s must exceed chassis ab_chord_window_s")

    recording = config.get("recording")
    if not isinstance(recording, dict):
        raise ValueError("control config missing recording object")
    recording_required = {
        "enabled", "output_dir", "fps", "double_click_s",
        "button_debounce_s", "camera_max_age_s", "camera_max_skew_s",
        "camera_max_frame_gap_s", "state_max_age_s", "max_rate_error_ratio",
        "encoder_queue_frames", "task", "video_codec", "video_preset",
        "video_crf",
    }
    recording_missing = sorted(recording_required.difference(recording))
    if recording_missing:
        raise ValueError(f"recording config missing keys: {', '.join(recording_missing)}")
    if not isinstance(recording["enabled"], bool):
        raise ValueError("recording enabled must be boolean")
    if not isinstance(recording["output_dir"], str) or not recording["output_dir"].strip():
        raise ValueError("recording output_dir must be a non-empty relative path")
    if Path(recording["output_dir"]).is_absolute() or ".." in Path(recording["output_dir"]).parts:
        raise ValueError("recording output_dir must stay inside the project")
    for key in (
        "fps", "double_click_s", "camera_max_age_s", "camera_max_skew_s",
        "camera_max_frame_gap_s", "state_max_age_s", "max_rate_error_ratio",
    ):
        if not math.isfinite(float(recording[key])) or float(recording[key]) <= 0.0:
            raise ValueError(f"recording {key} must be positive and finite")
    if not math.isfinite(float(recording["button_debounce_s"])) or not 0.0 <= float(
        recording["button_debounce_s"]
    ) < float(recording["double_click_s"]):
        raise ValueError("recording button_debounce_s must be within the double-click window")
    if not 5.0 <= float(recording["fps"]) <= 30.0:
        raise ValueError("recording fps must be within [5, 30]")
    if float(recording["fps"]) != 30.0:
        raise ValueError("recording fps must be exactly 30")
    if not 0.0 < float(recording["max_rate_error_ratio"]) <= 0.10:
        raise ValueError("recording max_rate_error_ratio must be within (0, 0.10]")
    if not isinstance(recording["encoder_queue_frames"], int) or not 4 <= int(
        recording["encoder_queue_frames"]
    ) <= 120:
        raise ValueError("recording encoder_queue_frames must be an integer within [4, 120]")
    camera_queue = recording.get("camera_queue_frames", 90)
    if type(camera_queue) is not int or not 16 <= camera_queue <= 300:
        raise ValueError("recording camera_queue_frames must be an integer within [16, 300]")
    if not isinstance(recording["task"], str) or not recording["task"].strip():
        raise ValueError("recording task must be non-empty")
    if recording["video_codec"] != "libx264":
        raise ValueError("recording video_codec must be libx264")
    if not isinstance(recording["video_preset"], str) or not recording["video_preset"]:
        raise ValueError("recording video_preset must be non-empty")
    if not isinstance(recording["video_crf"], int) or not 0 <= recording["video_crf"] <= 51:
        raise ValueError("recording video_crf must be an integer within [0, 51]")
    # YAML/session routing is authoritative; control.json retains motion/rate knobs.
    if (project_root() / "config/collection.yaml").exists():
        from pico_f1.collection import effective_settings
        collection = effective_settings(project_root())
        config["recording"] = dict(recording,
            output_dir=str(Path(collection["source"]).relative_to(project_root())),
            task=collection["task"])
    return config


def format_control_status(config: dict[str, Any] | None = None) -> str:
    """Return the stable one-line status printed by the shell launcher."""
    values = load_control_config() if config is None else config
    return (
        f"profile={values['profile']} "
        f"command_hz={float(values['command_hz']):g}"
    )
