"""Client configuration, independent of model and ROS dependencies."""
from pathlib import Path

from f1_replay.bundle import BODY_NAMES, decode_json
from f1_replay.runtime import ReplayError, finite_number, profile_issues
from . import CONTRACT

TASK = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = TASK / "configs/inference.json"


def load_config(path=DEFAULT_CONFIG, *, require_body_reference=True):
    path = Path(path).expanduser().resolve()
    doc = decode_json(path.read_bytes())
    if doc.get("contract") != CONTRACT:
        raise ReplayError("不是 F1 foam 推理合同")
    if doc.get("policy_contract") != "f1_state19_eef23_partial_base_v2":
        raise ReplayError("不是当前772条正式训练合同")
    cfg = doc["client"]
    for key in ("request_timeout_s", "max_observation_age_s", "camera_max_age_s",
                "camera_max_spread_s", "feedback_wait_s"):
        if not finite_number(cfg.get(key)) or cfg[key] <= 0:
            raise ReplayError(f"client.{key} 必须为有限正数")
    if not finite_number(cfg.get("camera_wait_s", 1.0)) or cfg.get("camera_wait_s", 1.0) <= 0:
        raise ReplayError("client.camera_wait_s 必须为有限正数")
    expired_limit = cfg.get("max_consecutive_expired", 3)
    if type(expired_limit) is not int or expired_limit < 1:
        raise ReplayError("client.max_consecutive_expired 必须为正整数")
    step_retries = cfg.get("first_arm_retry_limit", 0)
    if type(step_retries) is not int or not 0 <= step_retries <= 3:
        raise ReplayError("client.first_arm_retry_limit 必须为0..3的整数")
    native = cfg.get('inter_chunk_policy') in ('async_native_h50_v1', 'sync_raw_h50_v1')
    if cfg.get("frequency_hz") != 30 or cfg.get("execute_steps") != (50 if native else 20):
        raise ReplayError("30Hz合同：停车版H20执行，连续版H50动作队列")
    if cfg.get("inter_chunk_policy") not in ('stop_and_observe', 'async_native_h50_v1', 'sync_raw_h50_v1', 'sync_raw_h20_v1'):
        raise ReplayError("未知推理执行模式")
    if native:
        for name in ('prefetch_lookahead_steps', 'min_fresh_steps'):
            if type(cfg.get(name)) is not int or not 1 <= cfg[name] < 50:
                raise ReplayError(f'client.{name}需要1..49整数')
    topics = cfg.get("camera_topics", {})
    if (set(topics) != {"head", "left", "right"} or len(set(topics.values())) != 3
            or any(not isinstance(t, str) or not t.startswith("/") for t in topics.values())):
        raise ReplayError("需要head/left/right三个不同的相机绝对topic")
    profile_path = Path(cfg["runtime_profile"]).expanduser()
    if not profile_path.is_absolute():
        profile_path = path.parent / profile_path
    profile = decode_json(profile_path.read_bytes())
    overrides = cfg.get("profile_overrides", {})
    profile.update(overrides)
    from .execution import PREFIX_POLICY
    prefix_policy=profile.get('inference_prefix_policy','fixed')
    if prefix_policy not in ('fixed',PREFIX_POLICY):raise ReplayError('未知可执行前缀规则')
    pause=cfg.get('ik_pause_on_exhausted',False)
    if type(pause) is not bool:raise ReplayError('ik_pause_on_exhausted需要bool')
    if prefix_policy==PREFIX_POLICY:
        if cfg['inter_chunk_policy']!='sync_raw_h20_v1' or profile.get('inference_ik_recipe_version')!='f1_ikfk_demo_pos5mm_rot005deg_v1':
            raise ReplayError('可变前缀绑定5mm/0.05度同步H20')
    elif pause:raise ReplayError('IK暂停恢复需要可变前缀合同')
    first_arm_limit = profile.get("inference_first_arm_max_delta_deg", profile.get("start_arm_tolerance_deg"))
    first_arm_policy = profile.get("inference_first_arm_policy", "reject")
    if first_arm_policy not in ("reject", "report_only"):
        raise ReplayError("未知推理首步规则")
    if first_arm_policy == "reject" and (not finite_number(first_arm_limit) or first_arm_limit <= 0):
        raise ReplayError("推理首步关节差值范围必须为有限正数")
    if profile.get("inference_gripper_range_policy", "reject") not in ("reject", "saturate_0_100"):
        raise ReplayError("未知推理夹爪范围映射")
    if profile.get("inference_gripper_start_policy", "proximity") not in ("proximity", "target_position"):
        raise ReplayError("未知推理夹爪起步规则")
    angular_limit = profile.get("base_speed_limits", {}).get("wz", 0)
    if angular_limit:
        expected = profile.get("base_controller_expected_parameters", {}).get("max_speed.angular")
        if not finite_number(expected) or expected <= 0 or angular_limit > expected:
            raise ReplayError("非零推理wz范围必须绑定实际底盘角速度参数")
    # IK already verified its EEF residuals. Re-clipping would invalidate them.
    if profile.get("arm_limit_policy") != "reject":
        raise ReplayError("推理IK输出必须使用reject，不能再次clip")
    issues = profile_issues(profile)
    if issues:
        raise ReplayError("；".join(issues))
    profile["record_topics"] = list(topics.values())
    body_path = Path(cfg["body_reference"]).expanduser()
    if not body_path.is_absolute():
        body_path = path.parent / body_path
    if not require_body_reference:
        return doc, profile
    if not body_path.is_file():
        raise ReplayError("尚未绑定本机身体参考；现场摆好姿态后先运行 capture-body")
    body = decode_json(body_path.read_bytes())
    if (body.get("body_joint_names") != list(BODY_NAMES)
            or len(body.get("body_positions_deg", [])) != 5
            or not all(finite_number(v) for v in body["body_positions_deg"])):
        raise ReplayError("身体参考需要具名有限5D")
    if body.get("schema") == "f1_inference_body_reference_v1":
        if profile.get("expected_machine_id"):
            raise ReplayError("本机配置要求原生身体参考，不能使用旧示教角度")
        if not isinstance(body.get("manifest_sha256"), str) or len(body["manifest_sha256"]) != 64:
            raise ReplayError("示教身体参考缺少manifest SHA")
    elif body.get("schema") == "f1_native_body_reference_v1":
        from .body_reference import validate_reference
        validate_reference(body, profile)
    else:
        raise ReplayError("未知身体参考格式")
    profile["inference_body_reference"] = body
    return doc, profile
