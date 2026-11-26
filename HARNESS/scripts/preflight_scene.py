"""Repeatable read-only scene and joint-baseline preflight using formal gateway + SAM.

Strictly read-only: creates zero motion publishers, requests zero motion
authorization, denies all control IPC methods, and is never imported by any
production runtime path. Intended strictly as a pre-start readiness check before
an operator clicks Start in F1infra (4242).

Modes:
  1. Remote orchestration (default, run on 6000D):
     python3 HARNESS/scripts/preflight_scene.py --output <dir>
     Reads workspace.local.json, opens a temporary read-only SSH reverse tunnel
     for production SAM (127.0.0.1:19842) on an ephemeral/selected loopback port
     on ARM, runs this script in --local mode against the deployed frozen bundle
     and site on ARM, downloads result.json, prints an item-by-item report, and
     exits 0 on PASS or 2 on FAIL.

  2. Local ARM worker (--local):
     python3 scripts/preflight_scene.py --local --root <bundle> --site <site> \\
         --output <dir> [--seconds 12] [--acquire-timeout 48]
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen


DEFAULT_FULL_NAMES = (
    'arm_l_j1', 'arm_l_j2', 'arm_l_j3', 'arm_l_j4', 'arm_l_j5', 'arm_l_j6', 'arm_l_j7',
    'arm_r_j1', 'arm_r_j2', 'arm_r_j3', 'arm_r_j4', 'arm_r_j5', 'arm_r_j6', 'arm_r_j7',
    'body_j1', 'body_j2', 'body_j3', 'body_j4', 'body_j5',
    'head_j1', 'head_j2', 'head_j3',
)

ARM_NAMES = DEFAULT_FULL_NAMES[:14]

FASTDDS_UDP_ONLY_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
  <transport_descriptors>
    <transport_descriptor>
      <transport_id>udp_only</transport_id>
      <type>UDPv4</type>
    </transport_descriptor>
  </transport_descriptors>
  <participant profile_name="udp_participant" is_default_profile="true">
    <rtps>
      <useBuiltinTransports>false</useBuiltinTransports>
      <userTransports><transport_id>udp_only</transport_id></userTransports>
    </rtps>
  </participant>
</profiles>
"""


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_bytes())


def _save_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_joint_limits(
    numerics_doc: dict[str, Any],
    profile_doc: dict[str, Any] | None = None,
    joint_names: tuple[str, ...] = DEFAULT_FULL_NAMES,
) -> tuple[dict[str, list[float]], list[str]]:
    """Extract audited joint limits (deg) from profile + numerics and verify consistency.

    Returns (limits_by_name, mismatches). When profile_doc is provided, all 22
    joints are audited and the 14 arm limits in numerics_doc must match profile_doc.
    """
    mismatches: list[str] = []
    raw_num = numerics_doc['spec']['gate']['limits']
    num_limits: dict[str, list[float]] = {}
    if isinstance(raw_num, dict):
        for name, bounds in raw_num.items():
            num_limits[name] = [float(bounds[0]), float(bounds[1])]
    else:
        for idx, bounds in enumerate(raw_num):
            if idx < len(joint_names):
                num_limits[joint_names[idx]] = [float(bounds[0]), float(bounds[1])]

    if profile_doc is None:
        return num_limits, mismatches

    prof_raw = profile_doc.get('joint_limits_deg') or profile_doc.get('limits') or {}
    limits: dict[str, list[float]] = {}
    for name in joint_names:
        if name not in prof_raw:
            mismatches.append(f'profile_missing_limit:{name}')
            continue
        lo, hi = float(prof_raw[name][0]), float(prof_raw[name][1])
        limits[name] = [lo, hi]
        if name in num_limits:
            nlo, nhi = num_limits[name]
            if not (math.isclose(lo, nlo, abs_tol=1e-9) and math.isclose(hi, nhi, abs_tol=1e-9)):
                mismatches.append(f'numerics_profile_limit_mismatch:{name}:profile=[{lo},{hi}]:numerics=[{nlo},{nhi}]')
    return limits, mismatches


class JointLimitTracker:
    """Tracks min/max/last/violations across all audited joints from live feedback."""

    def __init__(self, limits_deg: dict[str, list[float]], joint_names: tuple[str, ...] = DEFAULT_FULL_NAMES):
        self.limits_deg = {k: [float(v[0]), float(v[1])] for k, v in limits_deg.items()}
        self.joint_names = tuple(joint_names)
        self.samples = 0
        self._stats: dict[str, dict[str, Any]] = {}
        for name in self.joint_names:
            bounds = self.limits_deg.get(name)
            self._stats[name] = {
                'limits_deg': bounds,
                'count': 0,
                'min_deg': None,
                'max_deg': None,
                'last_deg': None,
                'below_min_count': 0,
                'above_max_count': 0,
                'min_margin_deg': None,
            }

    def update(self, values: list[float] | tuple[float, ...]) -> list[str]:
        if len(values) != len(self.joint_names):
            raise ValueError(f'expected {len(self.joint_names)} joints, got {len(values)}')
        self.samples += 1
        violations: list[str] = []
        for name, raw in zip(self.joint_names, values):
            val = float(raw)
            st = self._stats[name]
            st['count'] += 1
            st['last_deg'] = val
            st['min_deg'] = val if st['min_deg'] is None else min(st['min_deg'], val)
            st['max_deg'] = val if st['max_deg'] is None else max(st['max_deg'], val)
            bounds = st['limits_deg']
            if bounds is not None:
                lo, hi = bounds
                margin = min(val - lo, hi - val)
                st['min_margin_deg'] = margin if st['min_margin_deg'] is None else min(st['min_margin_deg'], margin)
                if val < lo:
                    st['below_min_count'] += 1
                    violations.append(f'{name}={val:.4f}<{lo:.4f}')
                elif val > hi:
                    st['above_max_count'] += 1
                    violations.append(f'{name}={val:.4f}>{hi:.4f}')
        return violations

    def summary(self) -> dict[str, Any]:
        violating = {}
        for name, st in self._stats.items():
            if st['below_min_count'] > 0 or st['above_max_count'] > 0:
                violating[name] = {
                    'limits_deg': st['limits_deg'],
                    'min_deg': st['min_deg'],
                    'max_deg': st['max_deg'],
                    'last_deg': st['last_deg'],
                    'below_min_count': st['below_min_count'],
                    'above_max_count': st['above_max_count'],
                    'min_margin_deg': st['min_margin_deg'],
                }
        return {
            'total_samples': self.samples,
            'audited_joint_count': sum(1 for st in self._stats.values() if st['limits_deg'] is not None),
            'all_within_limits': self.samples > 0 and not violating,
            'violating_joints': violating,
            'joints': self._stats,
        }


def evaluate_scene_sample(
    obs: dict[str, Any],
    matches: dict[int, dict[str, Any]],
    scene_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate a single observe_with_status observation for pairing and 3D geometry."""
    by_camera: dict[str, Any] = {}
    for camera in ('head', 'right'):
        frames = [f for f in obs.get('frames', []) if f.get('camera') == camera and f.get('stream') in ('rgb', 'depth')]
        streams = {f['stream']: f for f in frames}
        pair = next((p for p in obs.get('pairings', []) if p.get('camera') == camera), None)
        times = [f['received_ns'] for f in frames]
        target = (min(times) + max(times)) // 2 if times else None
        feedback = matches.get(target) if target is not None else None
        matched_ns = feedback['state_ns'] if feedback else None
        skew_ms = max(abs(t - matched_ns) for t in times) / 1e6 if matched_ns is not None else None
        ok = bool(
            set(streams) == {'rgb', 'depth'}
            and pair
            and pair.get('quality') != 'invalid'
            and streams['rgb'].get('sequence') == pair.get('rgb_sequence')
            and streams['depth'].get('sequence') == pair.get('depth_sequence')
            and all(f.get('camera_info_sha256') and f.get('blob_sha256') for f in frames)
            and skew_ms is not None
            and skew_ms <= scene_cfg['frame_state_skew_ms']
        )
        by_camera[camera] = dict(
            good=ok,
            pair=pair,
            matched_feedback_ns=matched_ns,
            skew_ms=skew_ms,
            frames=[
                {
                    k: f.get(k)
                    for k in ('stream', 'sequence', 'source_ns', 'received_ns', 'blob_sha256', 'camera_info_sha256')
                }
                for f in frames
            ],
        )

    geometry = scene_cfg['geometry']

    def _measured(object_id: str) -> dict[str, Any] | None:
        obj = next((x for x in obs.get('objects', []) if x.get('object_id') == object_id), {})
        pose = obj.get('pose')
        if (
            not obj.get('visible')
            or obj.get('frame_id') != geometry['native_frame']
            or obj.get('shape') != 'square'
            or not obj.get('evidence_ids')
            or not pose
            or len(pose) != 16
            or not all(math.isfinite(x) for x in pose)
        ):
            return None
        return {k: obj[k] for k in ('object_id', 'frame_id', 'pose', 'evidence_ids', 'shape', 'visible')}

    block = _measured(geometry['object_id'])
    hole = _measured(geometry['hole_id'])
    fixture = obs.get('fixture') or {}
    measured_fixture = bool(
        fixture.get('source') == 'rgbd_anchor_registration'
        and fixture.get('evidence_ids')
        and len(fixture.get('pose', [])) == 16
        and all(math.isfinite(x) for x in fixture['pose'])
    )
    valid = obs.get('state_valid_mask') or []
    base_ok = (
        not obs.get('synthetic', True)
        and len(valid) == 16
        and all(valid[7:14])
        and obs.get('calibration_id') == geometry['calibration_id']
        and all(p['good'] for p in by_camera.values())
    )
    return dict(
        observation_id=obs.get('observation_id'),
        received_ns=obs.get('received_ns'),
        cameras=by_camera,
        valid_dual_rgbd_feedback=bool(base_ok),
        geometry_scope=obs.get('geometry_scope'),
        block=block,
        hole=hole,
        fixture=fixture if measured_fixture else None,
        fixture_raw=fixture,
        block_solved=bool(block),
        hole_solved=bool(hole),
        fixture_solved=bool(measured_fixture),
        full_geometry=bool(base_ok and block and hole and measured_fixture),
        faults=obs.get('faults', []),
        warnings=obs.get('quality_warnings', []),
    )


def evaluate_left_wrist_window(
    timestamps_s: list[float],
    window_start_s: float,
    window_end_s: float,
    *,
    min_span_s: float = 10.0,
    min_frames: int = 10,
    max_gap_s: float = 2.0,
) -> dict[str, Any]:
    """Evaluate left-wrist camera stream continuity inside [window_start_s, window_end_s]."""
    in_window = sorted(t for t in timestamps_s if window_start_s <= t <= window_end_s)
    count = len(in_window)
    span_s = (in_window[-1] - in_window[0]) if count >= 2 else 0.0
    gaps = [b - a for a, b in zip(in_window, in_window[1:])]
    if count >= 1:
        lead_gap = max(0.0, in_window[0] - window_start_s)
        tail_gap = max(0.0, window_end_s - in_window[-1])
        max_gap = max([lead_gap, tail_gap, *gaps])
    else:
        max_gap = max(0.0, window_end_s - window_start_s)
    passed = count >= min_frames and span_s >= min_span_s and max_gap <= max_gap_s
    return {
        'passed': passed,
        'frame_count': count,
        'span_s': span_s,
        'max_gap_s': max_gap,
        'min_span_s': min_span_s,
        'min_frames': min_frames,
        'max_allowed_gap_s': max_gap_s,
    }


def evaluate_preflight(
    *,
    required_seconds: float,
    joint_summary: dict[str, Any],
    limit_mismatches: list[str],
    fk_check: dict[str, Any] | None,
    contiguous_paired_s: float,
    unique_paired_scenes: int,
    rgbd_frame_advancement_s: dict[str, float],
    left_wrist_eval: dict[str, Any],
    geometry_counts: dict[str, int],
    geometry_examples: list[dict[str, Any]],
    sam_requests_delta: int | None,
    readonly_invariants: dict[str, Any],
) -> dict[str, Any]:
    """Compute item-by-item pass/fail checks and overall preflight verdict."""
    reasons: list[str] = []

    # 1. All joints within audited limits (plus live FK acceptance)
    fk_ok = bool(fk_check and fk_check.get('ok') is True)
    joints_reasons: list[str] = []
    if joint_summary['total_samples'] <= 0:
        joints_reasons.append('no_valid_joint_feedback_received')
    if limit_mismatches:
        joints_reasons.extend(limit_mismatches)
    if joint_summary['violating_joints']:
        for name, info in sorted(joint_summary['violating_joints'].items()):
            lo, hi = info['limits_deg']
            joints_reasons.append(
                f'joint_out_of_limits:{name}:observed=[{info["min_deg"]:.4f},{info["max_deg"]:.4f}]:limits=[{lo:.4f},{hi:.4f}]'
            )
    if fk_check is None:
        joints_reasons.append('live_fk_not_executed')
    elif not fk_ok:
        joints_reasons.append(f'live_fk_rejected:{fk_check.get("error_type")}:{fk_check.get("error")}')
    joints_pass = not joints_reasons
    reasons.extend(joints_reasons)

    # 2. Head & right-wrist RGB-D paired with same-timestamp joint feedback >= required_seconds
    pair_reasons: list[str] = []
    min_adv_required = min(required_seconds, 10.0)
    if contiguous_paired_s < required_seconds:
        pair_reasons.append(f'contiguous_paired_window_short:{contiguous_paired_s:.2f}s<{required_seconds:.2f}s')
    if unique_paired_scenes < 2:
        pair_reasons.append(f'insufficient_unique_paired_scenes:{unique_paired_scenes}')
    expected_streams = ('head.rgb', 'head.depth', 'right.rgb', 'right.depth')
    for stream in expected_streams:
        adv = rgbd_frame_advancement_s.get(stream, 0.0)
        if adv < min_adv_required:
            pair_reasons.append(f'rgbd_stream_advancement_short:{stream}:{adv:.2f}s<{min_adv_required:.2f}s')
    pair_pass = not pair_reasons
    reasons.extend(pair_reasons)

    # 3. Left wrist streaming continuously
    left_reasons: list[str] = []
    if not left_wrist_eval.get('passed'):
        left_reasons.append(
            f'left_wrist_not_continuous:frames={left_wrist_eval.get("frame_count")},'
            f'span={left_wrist_eval.get("span_s", 0.0):.2f}s,'
            f'max_gap={left_wrist_eval.get("max_gap_s", 0.0):.2f}s'
        )
    left_pass = not left_reasons
    reasons.extend(left_reasons)

    # 4. 3D geometry solved for block and bucket/fixture via SAM (square hole reported separately)
    geom_reasons: list[str] = []
    if geometry_counts.get('block', 0) <= 0:
        geom_reasons.append('3d_geometry_missing:block')
    if geometry_counts.get('fixture', 0) <= 0:
        geom_reasons.append('3d_geometry_missing:bucket_fixture')
    if sam_requests_delta is not None and sam_requests_delta <= 0:
        geom_reasons.append(f'sam_no_inference_requests:delta={sam_requests_delta}')
    geom_pass = not geom_reasons
    reasons.extend(geom_reasons)

    # 5. Read-only safety invariants
    ro_reasons: list[str] = []
    if readonly_invariants.get('hardware_command_publishers', 0) != 0:
        ro_reasons.append('unexpected_hardware_command_publisher')
    if readonly_invariants.get('denied_ipc'):
        ro_reasons.append('unexpected_control_ipc_attempt')
    if readonly_invariants.get('final_authorized') is not False:
        ro_reasons.append('unexpected_motion_authorization')
    if readonly_invariants.get('request_publications', 0) != 0:
        ro_reasons.append('unexpected_action_publication')
    if readonly_invariants.get('start_sent') is not False:
        ro_reasons.append('unexpected_start_sent')
    ro_pass = not ro_reasons
    reasons.extend(ro_reasons)

    checks = {
        'joints_within_limits': {
            'passed': joints_pass,
            'reasons': joints_reasons,
            'audited_joint_count': joint_summary['audited_joint_count'],
            'total_samples': joint_summary['total_samples'],
            'violating_joints': joint_summary['violating_joints'],
            'arm_l_j2': joint_summary['joints'].get('arm_l_j2'),
            'fk_ok': fk_ok,
        },
        'dual_rgbd_feedback_paired': {
            'passed': pair_pass,
            'reasons': pair_reasons,
            'contiguous_s': contiguous_paired_s,
            'required_s': required_seconds,
            'unique_paired_scenes': unique_paired_scenes,
            'rgbd_frame_advancement_s': rgbd_frame_advancement_s,
        },
        'left_wrist_streaming': {
            'passed': left_pass,
            'reasons': left_reasons,
            **left_wrist_eval,
        },
        'scene_geometry_solved': {
            'passed': geom_pass,
            'reasons': geom_reasons,
            'counts': geometry_counts,
            'hole_direct_count': geometry_counts.get('hole', 0),
            'full_scene_count': geometry_counts.get('full', 0),
            'sam_requests_delta': sam_requests_delta,
            'example_count': len(geometry_examples),
        },
        'readonly_safety': {
            'passed': ro_pass,
            'reasons': ro_reasons,
            **readonly_invariants,
        },
    }
    passed = all(item['passed'] for item in checks.values())
    return {'passed': passed, 'reasons': reasons, 'checks': checks}


def format_report(report: dict[str, Any]) -> str:
    """Format a concise human-readable item-by-item preflight report."""
    verdict = 'PASS' if report.get('passed') else 'FAIL'
    lines = [
        f'=== F1 Harness Read-Only Scene Preflight: {verdict} ===',
        f"Bundle: {report.get('bundle_id', 'unknown')[:12]}  Product: {report.get('product_version', 'unknown')}",
        f"RMW: {report.get('rmw', 'unknown')}  Duration target: {report.get('required_contiguous_s')}s",
    ]
    checks = report.get('checks') or {}
    labels = [
        ('joints_within_limits', '1. All joints within audited limits + live FK'),
        ('dual_rgbd_feedback_paired', '2. Head & right-wrist RGB-D paired with joint feedback'),
        ('left_wrist_streaming', '3. Left-wrist camera continuous frames'),
        ('scene_geometry_solved', '4. 3D geometry solved (block, bucket/fixture, square hole)'),
        ('readonly_safety', '5. Read-only invariants (0 actions, 0 auth)'),
    ]
    for key, title in labels:
        item = checks.get(key)
        if not item:
            lines.append(f'[SKIP] {title}')
            continue
        tag = 'PASS' if item.get('passed') else 'FAIL'
        lines.append(f'[{tag}] {title}')
        if key == 'joints_within_limits':
            al2 = item.get('arm_l_j2') or {}
            lines.append(
                f"       samples={item.get('total_samples')} audited={item.get('audited_joint_count')} "
                f"arm_l_j2=[{al2.get('min_deg')}, {al2.get('max_deg')}] limit={al2.get('limits_deg')} fk_ok={item.get('fk_ok')}"
            )
        elif key == 'dual_rgbd_feedback_paired':
            adv = item.get('rgbd_frame_advancement_s') or {}
            adv_str = ', '.join(f'{k}={v:.2f}s' for k, v in sorted(adv.items()))
            lines.append(
                f"       contiguous={item.get('contiguous_s', 0.0):.2f}s unique_scenes={item.get('unique_paired_scenes')} "
                f"advancement=({adv_str})"
            )
        elif key == 'left_wrist_streaming':
            lines.append(
                f"       frames={item.get('frame_count')} span={item.get('span_s', 0.0):.2f}s "
                f"max_gap={item.get('max_gap_s', 0.0):.2f}s"
            )
        elif key == 'scene_geometry_solved':
            cnt = item.get('counts') or {}
            lines.append(
                f"       block={cnt.get('block', 0)} fixture={cnt.get('fixture', 0)} "
                f"hole={cnt.get('hole', 0)} complete={cnt.get('full', 0)} sam_delta={item.get('sam_requests_delta')}"
            )
        elif key == 'readonly_safety':
            lines.append(
                f"       cmd_pubs={item.get('hardware_command_publishers')} "
                f"authorized={item.get('final_authorized')} pubs={item.get('request_publications')}"
            )
        for reason in item.get('reasons') or []:
            lines.append(f'       -> {reason}')

    if report.get('error'):
        lines.append(f"Error: {report['error']}")
    if not report.get('passed'):
        lines.append('Verdict: FAIL - do NOT click Start in 4242 until blocking items above are resolved.')
    else:
        lines.append('Verdict: PASS - scene & joint preflight ready for operator-initiated run in 4242.')
    return '\n'.join(lines)


def _run_local_worker(args: argparse.Namespace) -> int:
    """Execute passive gateway + SAM preflight locally on the ARM robot."""
    if not 10 <= args.seconds <= 60 or not args.seconds + 5 <= args.acquire_timeout <= 180:
        raise SystemExit('require 10-60s continuous sampling with bounded acquire-timeout')

    root = args.root.resolve()
    site = args.site.resolve()
    out = args.output.resolve()
    manifest = _read_json(root / 'manifest.json')
    if args.bundle_id and manifest['bundle_id'] != args.bundle_id:
        raise ValueError(f"bundle_id mismatch: expected {args.bundle_id}, found {manifest['bundle_id']}")

    sys.path[:0] = [str(root / rel) for rel in ('deps', 'src', 'control/src', 'upstream/RPent')]
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.utilities import get_rmw_implementation_identifier
    from sensor_msgs.msg import CompressedImage, JointState
    from f1dagger.contract import FULL_NAMES
    from f1_harness.contracts import Mode, Observation
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.ipc import UnixClient, UnixServer
    from f1_harness.owner_bridge import OwnerBridge
    from f1_harness.site import _safe_environment
    from rpent.utils.rpc.http_rpc import HttpRpcClient

    out.mkdir(parents=True, exist_ok=False)
    cfgdir = out / 'site'
    cfgdir.mkdir(mode=0o700)
    runtime = Path(tempfile.mkdtemp(prefix='f1-preflight-scene-'))

    report: dict[str, Any] = dict(
        schema='f1-harness-preflight-scene-v1',
        bundle_id=manifest['bundle_id'],
        product_version=manifest.get('product_version', 'unknown'),
        root=str(root),
        site=str(site),
        started=time.time(),
        passed=False,
        required_contiguous_s=args.seconds,
        acquire_timeout_s=args.acquire_timeout,
        gateway_rpc='observe_with_status',
        start_sent=False,
        hardware_owner_connected=False,
        hardware_command_publishers=0,
        feedback_topic='/hal/joint_states',
        rmw=get_rmw_implementation_identifier(),
        middleware_environment={
            k: os.environ.get(k)
            for k in (
                'ROS_DOMAIN_ID',
                'ROS_AUTOMATIC_DISCOVERY_RANGE',
                'RMW_IMPLEMENTATION',
                'CYCLONEDDS_URI',
                'FASTRTPS_DEFAULT_PROFILES_FILE',
            )
        },
        files={},
        samples=[],
        children={},
        denied_ipc=[],
        rationale='Read-only preflight; HTTP/IPC/cache relocated; zero motion publishers or authorization.',
    )

    children: list[tuple[str, subprocess.Popen]] = []
    logs = []
    ipc = engine = bridge = ros = executor = spin = None
    counters: Counter = Counter()
    callbacks: Counter = Counter()
    seqs: dict[str, int] = {}
    matches: dict[int, dict[str, Any]] = {}
    match_lock = threading.Lock()
    left_times: list[float] = []
    scope_id = None
    capture_fk = threading.Event()
    fk_input: dict[str, Any] = {}
    fk_worker = None
    fk_record: dict[str, Any] | None = None
    tracker: JointLimitTracker | None = None
    limit_mismatches: list[str] = []
    sam_start_requests: int | None = None
    sam_end_requests: int | None = None
    best_window_start: float | None = None
    best_window_end: float | None = None
    best_longest = 0.0
    best_distinct: set[str] = set()
    best_stream_adv: dict[str, float] = {}
    best_geom_counts: Counter = Counter()
    best_geom_examples: list[dict[str, Any]] = []

    try:
        gateway = _read_json(site / 'gateway.local.json')
        cfg = gateway['scene']
        for src_json in sorted(site.glob('*.json')):
            shutil.copyfile(src_json, cfgdir / src_json.name)
            report['files'][src_json.name] = dict(original=_sha256(src_json))

        dev_orig = _read_json(cfgdir / 'development.local.json')
        protected = dev_orig['reviewed_files']
        for name, expected_sha in protected.items():
            if name not in report['files'] or expected_sha != report['files'][name]['original']:
                raise ValueError('site review hash differs: ' + name)

        # If the robot rebooted since calibration capture and the template site has no boot_recheck,
        # run the formal read-only boot-recheck on the shadow cfgdir (same ROS env as web_process.py:122-128).
        calib_path = cfgdir / dev_orig['calibration']['path']
        calib_boot = _read_json(calib_path).get('boot_id') if calib_path.is_file() else None
        current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip() if Path('/proc/sys/kernel/random/boot_id').is_file() else None
        if current_boot and calib_boot and calib_boot != current_boot and not dev_orig.get('boot_recheck'):
            boot_setups = (
                '/opt/ros/jazzy/setup.bash',
                '/home/user/workspace/f1/install/setup.bash',
                '/home/user/workspace/agv/install/setup.bash',
                '/home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.bash',
                '/home/user/workspace/camera/ros2_ws/install/setup.bash',
            )
            boot_env_script = (
                'unset RMW_IMPLEMENTATION FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE '
                'CYCLONEDDS_URI ROS_LOCALHOST_ONLY ROS_STATIC_PEERS\n'
                + ''.join(f'source {shlex.quote(s)} || exit\n' for s in boot_setups)
                + 'exec ' + shlex.join([sys.executable, str(root / 'native/entry.py'), '--run', str(out / 'boot-recheck'), 'boot-recheck', str(cfgdir)])
            )
            boot_proc = subprocess.run(['bash', '-c', boot_env_script], text=True, capture_output=True, timeout=40)
            (out / 'boot-recheck.stdout.txt').write_text(boot_proc.stdout, encoding='utf-8')
            (out / 'boot-recheck.stderr.txt').write_text(boot_proc.stderr, encoding='utf-8')
            if boot_proc.returncode != 0:
                raise RuntimeError('shadow boot-recheck failed: ' + (boot_proc.stderr or boot_proc.stdout)[-1200:])
            report['boot_recheck_performed'] = True

        masks_doc = _read_json(cfgdir / 'masks.local.json')
        masks = masks_doc['views']
        urls = {v['sam_url'] for v in masks.values() if v.get('backend') == 'sam2_remote'}
        checkpoints = {v['checkpoint_sha256'] for v in masks.values()}
        if (
            cfg.get('mask_execution') != 'local_sam2'
            or len(urls) != 1
            or len(checkpoints) != 1
            or any(v.get('backend') != 'sam2_remote' for v in masks.values())
        ):
            raise ValueError('site must bind real SAM for all views')

        sam_url = args.sam_url or urls.pop()
        parsed = urlsplit(sam_url)
        if parsed.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('SAM URL must be loopback')
        report['sam_url'] = sam_url
        with urlopen(sam_url.rstrip('/') + '/health', timeout=5) as response:
            sam_health = json.load(response)
        report['sam_health_start'] = sam_health
        sam_start_requests = sam_health.get('requests')
        if sam_health.get('ready') is not True or sam_health.get('model_sha256') not in checkpoints:
            raise ValueError('production SAM model identity unavailable')

        if args.sam_url:
            for view in masks.values():
                view['sam_url'] = sam_url
            _save_json(cfgdir / 'masks.local.json', masks_doc)
            cfg['mask_url'] = sam_url

        sockets = []
        try:
            for k in ('control_port', 'image_port'):
                s = socket.socket()
                s.bind(('127.0.0.1', 0))
                sockets.append(s)
                gateway[k] = s.getsockname()[1]
        finally:
            for s in sockets:
                s.close()

        gateway['owner_socket'] = str(runtime / 'feedback.sock')
        cfg.update(
            owner_socket=gateway['owner_socket'],
            numerical_socket=str(runtime / 'numerics.sock'),
            verifier_socket=str(runtime / 'verify.sock'),
        )
        if isinstance(cfg.get('frame_chain'), dict) and 'development_owner_config' in cfg['frame_chain']:
            cfg['frame_chain']['development_owner_config'] = str(cfgdir / 'owner.local.json')
        _save_json(cfgdir / 'gateway.local.json', gateway)

        numerics_doc = _read_json(cfgdir / 'numerics.local.json')
        for kind in ('numerics', 'verify'):
            p = cfgdir / (kind + '.local.json')
            edited = _read_json(p)
            edited['socket'] = str(runtime / (kind + '.sock'))
            if kind == 'numerics':
                edited['spec']['gate']['runtime'] = str(out / 'numerical-gate-cache')
            _save_json(p, edited)

        launch_path = cfgdir / 'launch.local.json'
        profile_path = args.profile
        if launch_path.exists():
            launch_doc = _read_json(launch_path)
            if profile_path is None and launch_doc.get('profile'):
                profile_path = Path(launch_doc['profile'])
            launch_doc['runtime'] = str(out / 'model-cache')
            _save_json(launch_path, launch_doc)

        profile_doc = None
        if profile_path is not None and profile_path.is_file():
            profile_doc = _read_json(profile_path)
            report['profile_path'] = str(profile_path)
            report['profile_sha256'] = _sha256(profile_path)
        limits_deg, limit_mismatches = parse_joint_limits(numerics_doc, profile_doc, tuple(FULL_NAMES))
        report['limit_mismatches'] = limit_mismatches
        tracker = JointLimitTracker(limits_deg, tuple(FULL_NAMES))

        # Re-seal reviewed_files ONLY in the temporary shadow cfgdir after port/socket/SAM relocation
        # (original reviewed_files were already verified against the immutable site above).
        dev_shadow = _read_json(cfgdir / 'development.local.json')
        for name in list(dev_shadow.get('reviewed_files', {})):
            if (cfgdir / name).is_file():
                dev_shadow['reviewed_files'][name] = _sha256(cfgdir / name)
        _save_json(cfgdir / 'development.local.json', dev_shadow)

        for name, row in report['files'].items():
            row['shadow'] = _sha256(cfgdir / name)

        def replicate_fk():
            nonlocal fk_record
            if not capture_fk.wait(timeout=args.acquire_timeout + 15) or not fk_input:
                return
            record = dict(fk_input, method='forward_pose', issued_monotonic_ns=time.monotonic_ns())
            try:
                result = UnixClient(Path(cfg['numerical_socket']), timeout=3.0).call(
                    'forward_pose', full22=fk_input['full22'], held16=fk_input['measured16']
                )
                record['ok'] = True
                record['result'] = result
            except Exception as exc:
                record['ok'] = False
                record['error_type'] = type(exc).__name__
                record['error'] = str(exc)
            record['completed_monotonic_ns'] = time.monotonic_ns()
            fk_record = record
            report['live_fk_check'] = record

        engine = TaskEngine(EvidenceStore(out / 'passive-owner'), mode=Mode.READ_ONLY)
        scope_id = engine.instance
        bridge = OwnerBridge(
            engine,
            numerical_planner=None,
            result_verifier=None,
            feedback_age_ns=int(cfg['feedback_age_ms'] * 1e6),
            reference_tolerance_deg=cfg['reference_tolerance_deg'],
        )

        def dispatch(method, kwargs):
            counters[method] += 1
            if method == 'status':
                return engine.status()
            if method == 'measured_feedback':
                answer = bridge.measured_feedback(**kwargs)
                if kwargs.get('target_received_ns') is not None:
                    with match_lock:
                        t = answer['matched_state_received_ns']
                        matches[kwargs['target_received_ns']] = dict(state_ns=t)
                        if not fk_input:
                            fk_input.update(
                                target_received_ns=kwargs['target_received_ns'],
                                state_received_ns=t,
                                full22=list(answer['full22']),
                                measured16=list(answer['measured16']),
                                actual_stamps16=list(answer['stamps16']),
                            )
                            capture_fk.set()
                return answer
            if method == 'invalidate_live_scene':
                return engine.invalidate_live_observation(kwargs['reason'])
            if method == 'ingest_live_scene':
                return engine.update_live_observation(Observation.model_validate(kwargs['observation']), kwargs['context'])
            if method == 'ingest_observation':
                return engine.update_observation(Observation.model_validate(kwargs['observation']))
            report['denied_ipc'].append(method)
            raise PermissionError('read-only preflight has no control interface')

        ipc = UnixServer(runtime / 'feedback.sock', dispatch, scene_evidence=True).start()
        rclpy.init(args=None)
        ros = rclpy.create_node('f1_preflight_passive_feedback_' + str(os.getpid()), enable_rosout=False)
        executor = SingleThreadedExecutor()
        executor.add_node(ros)

        def joint_cb(msg):
            stamp = time.monotonic_ns()
            names = dict(zip(msg.name, msg.position))
            if len(msg.name) != len(msg.position) or len(names) != len(msg.name) or not set(FULL_NAMES) <= names.keys():
                callbacks['incomplete_joints'] += 1
                return
            values = [float(names[name]) for name in FULL_NAMES]
            if not all(math.isfinite(v) for v in values):
                callbacks['nonfinite_joints'] += 1
                return
            with match_lock:
                tracker.update(values)
            bridge.update_feedback(full22=values, measured16=values[:14] + [0.0, 0.0], stamps16=[stamp] * 14 + [None, None])
            callbacks['valid_joints'] += 1

        def left_cb(msg):
            if not msg.data:
                return
            stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
            if seqs.get('left_source_ns') != stamp:
                left_times.append(time.monotonic())
                seqs['left_source_ns'] = stamp
                callbacks['left_images'] += 1

        left_topic = next(c['rgb']['topic'] for c in gateway['observation']['inspection_cameras'] if c['name'] == 'left')
        report['left_topic'] = left_topic
        ros.create_subscription(JointState, '/hal/joint_states', joint_cb, qos_profile_sensor_data)
        ros.create_subscription(CompressedImage, left_topic, left_cb, qos_profile_sensor_data)
        spin = threading.Thread(target=executor.spin, daemon=True)
        spin.start()
        fk_worker = threading.Thread(target=replicate_fk, daemon=True)
        fk_worker.start()

        for kind in ('verify', 'numerics', 'gateway'):
            logfile = out / (kind + '.log')
            handle = logfile.open('wb')
            logs.append(handle)
            cmd = [sys.executable, str(root / 'native/entry.py'), '--run', str(out / kind), 'serve', kind, str(cfgdir / (kind + '.local.json'))]
            child = subprocess.Popen(
                cmd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=_safe_environment(),
            )
            children.append((kind, child))
            report['children'][kind] = {'argv': cmd, 'pid': child.pid}
            end = time.monotonic() + 70
            while time.monotonic() < end:
                if child.poll() is not None:
                    raise RuntimeError(kind + ' exited: ' + logfile.read_text(errors='replace')[-1300:])
                if '"ready": true' in logfile.read_text(errors='replace'):
                    break
                time.sleep(0.12)
            else:
                raise TimeoutError('native service readiness timeout: ' + kind)

        rpc = HttpRpcClient('http://127.0.0.1:' + str(gateway['control_port']), enable_sessions=False)
        capture_start = time.monotonic()
        deadline = capture_start + args.acquire_timeout
        window: float | None = None
        distinct: set[str] = set()
        stream_first: dict[str, int] = {}
        stream_latest: dict[str, int] = {}
        geom_counts: Counter = Counter()
        geom_examples: list[dict[str, Any]] = []
        states: Counter = Counter()

        while time.monotonic() < deadline:
            if any(child.poll() is not None for _, child in children):
                raise RuntimeError('native auxiliary service exited during preflight')
            stamp = time.monotonic()
            entry: dict[str, Any] = {'at_s': stamp}
            try:
                snapshot = rpc.call('observe_with_status', timeout_s=3.0)
                status = snapshot['status']
                if status['owner_instance_id'] != scope_id or status['authorized'] or status['running'] or status['pending']:
                    raise RuntimeError('unexpected owner/authorization state')
                obs = snapshot['observation']
                with match_lock:
                    entry.update(evaluate_scene_sample(obs, matches, cfg))
                if entry['valid_dual_rgbd_feedback']:
                    if window is None:
                        window = stamp
                        distinct = set()
                        stream_first = {}
                        stream_latest = {}
                        geom_counts = Counter()
                        geom_examples = []
                    for camera, details in entry['cameras'].items():
                        for frame in details['frames']:
                            name = camera + '.' + frame['stream']
                            value = frame['received_ns']
                            stream_first.setdefault(name, value)
                            stream_latest[name] = value
                    distinct.add(obs['observation_id'])
                    if entry['block_solved']:
                        geom_counts['block'] += 1
                    if entry['fixture_solved']:
                        geom_counts['fixture'] += 1
                    if entry['hole_solved']:
                        geom_counts['hole'] += 1
                    if entry['full_geometry']:
                        geom_counts['full'] += 1
                        if len(geom_examples) < 3:
                            geom_examples.append(
                                dict(
                                    observation_id=obs['observation_id'],
                                    block=entry['block'],
                                    hole=entry['hole'],
                                    fixture=entry['fixture'],
                                )
                            )
                else:
                    window = None
            except Exception as exc:
                window = None
                entry['error'] = str(exc)[:700]

            states[entry.get('error') or ('full_geometry' if entry.get('full_geometry') else ('paired' if entry.get('valid_dual_rgbd_feedback') else 'partial'))] += 1
            report['samples'].append(entry)

            if window is not None:
                elapsed = stamp - window
                adv = {name: (stream_latest[name] - first) / 1e9 for name, first in stream_first.items()}
                if elapsed >= best_longest:
                    best_longest = elapsed
                    best_window_start = window
                    best_window_end = stamp
                    best_distinct = set(distinct)
                    best_stream_adv = dict(adv)
                    best_geom_counts = Counter(geom_counts)
                    best_geom_examples = list(geom_examples)
                left_eval_now = evaluate_left_wrist_window(
                    left_times,
                    window,
                    stamp,
                    min_span_s=min(args.seconds, 10.0),
                    max_gap_s=args.max_left_gap_s,
                )
                if (
                    elapsed >= args.seconds
                    and len(distinct) > 1
                    and geom_counts['full'] > 0
                    and left_eval_now['passed']
                    and len(adv) == 4
                    and min(adv.values()) >= min(args.seconds, 10.0)
                    and (fk_record is not None or not capture_fk.is_set())
                ):
                    break
            time.sleep(0.18)

        capture_end = time.monotonic()
        with urlopen(sam_url.rstrip('/') + '/health', timeout=5) as response:
            sam_end_health = json.load(response)
        report['sam_health_end'] = sam_end_health
        sam_end_requests = sam_end_health.get('requests')
        sam_delta = (sam_end_requests - sam_start_requests) if (sam_start_requests is not None and sam_end_requests is not None) else None

        try:
            inspection = rpc.call('camera_observation', timeout_s=3.0)
            report['gateway_inspection_rgb'] = [
                dict(camera=f['camera'], sequence=f['sequence'], source_ns=f['source_ns'])
                for f in inspection.get('frames', [])
            ]
        except Exception as exc:
            report['gateway_inspection_error'] = str(exc)

        report['feedback_ros_publishers'] = [p.topic_name for p in ros.publishers]
        report['topic_publishers'] = {
            t: ros.count_publishers(t)
            for t in set(
                [left_topic, '/hal/joint_states']
                + [v[k]['topic'] for v in gateway['observation']['cameras'] for k in ('rgb', 'depth')]
            )
        }
        hw_pubs = [p for p in report['feedback_ros_publishers'] if p.startswith(('/motion_ctl/', '/move/'))]
        report['hardware_command_publishers'] = len(hw_pubs)
        report['request_outcomes'] = dict(states)
        report['geometry_examples'] = best_geom_examples

        # Ensure live FK check completes if feedback arrived
        capture_fk.set()
        if fk_worker:
            fk_worker.join(timeout=4)

        win_start = best_window_start if best_window_start is not None else capture_start
        win_end = best_window_end if best_window_end is not None else capture_end
        left_eval = evaluate_left_wrist_window(
            left_times,
            win_start,
            win_end,
            min_span_s=min(args.seconds, 10.0),
            max_gap_s=args.max_left_gap_s,
        )

        with match_lock:
            joint_summary = tracker.summary() if tracker is not None else {'total_samples': 0, 'audited_joint_count': 0, 'all_within_limits': False, 'violating_joints': {}, 'joints': {}}
        report['joint_summary'] = joint_summary

        readonly_invariants = {
            'hardware_command_publishers': report['hardware_command_publishers'],
            'denied_ipc': list(report['denied_ipc']),
            'final_authorized': bool(engine.authorized),
            'request_publications': len(engine.request_publications),
            'start_sent': False,
        }
        verdict = evaluate_preflight(
            required_seconds=args.seconds,
            joint_summary=joint_summary,
            limit_mismatches=limit_mismatches,
            fk_check=fk_record,
            contiguous_paired_s=best_longest,
            unique_paired_scenes=len(best_distinct),
            rgbd_frame_advancement_s=best_stream_adv,
            left_wrist_eval=left_eval,
            geometry_counts=dict(best_geom_counts),
            geometry_examples=best_geom_examples,
            sam_requests_delta=sam_delta,
            readonly_invariants=readonly_invariants,
        )
        report.update(verdict)
    except Exception as exc:
        report['passed'] = False
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        capture_fk.set()
        if fk_worker:
            fk_worker.join(timeout=3)
        for kind, child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGINT)
                try:
                    child.wait(timeout=9)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            report['children'][kind]['exit_code'] = child.returncode
        for log in logs:
            log.close()
        if ipc:
            ipc.close()
        if executor:
            executor.shutdown(timeout_sec=2.0)
        if spin:
            spin.join(timeout=2)
        if ros:
            ros.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        if bridge:
            bridge.executor.shutdown(wait=False, cancel_futures=True)
        if engine:
            report.update(final_authorized=engine.authorized, request_publications=len(engine.request_publications))
        report['callback_counts'] = dict(callbacks)
        report['owner_ipc_methods'] = dict(counters)
        report['finished'] = time.time()
        _save_json(out / 'result.json', report)
        shutil.rmtree(runtime, ignore_errors=True)

    print(format_report(report), flush=True)
    return 0 if report['passed'] else 2


# Must stay aligned with start_development.ROBOT_SERVICES_ROS_ENV (verified by unit test).
PRODUCTION_GATEWAY_ROS_ENV: str | None = None


def _build_remote_ros_prelude(
    ros_env: str,
    remote_bundle: str | None = None,
    *,
    production_ros_env: str | None = None,
) -> str:
    if ros_env == 'production-gateway':
        unset_line = (
            'unset RMW_IMPLEMENTATION FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE '
            'CYCLONEDDS_URI ROS_LOCALHOST_ONLY ROS_STATIC_PEERS\n'
        )
        selected = PRODUCTION_GATEWAY_ROS_ENV if production_ros_env is None else production_ros_env
        if selected is None:
            return unset_line + 'source /opt/ros/jazzy/setup.bash || exit\n'
        if selected == 'fastdds-udp':
            if not remote_bundle:
                raise ValueError('remote_bundle required for fastdds-udp production-gateway prelude')
            return (
                unset_line
                + 'source /opt/ros/jazzy/setup.bash || exit\n'
                + 'export RMW_IMPLEMENTATION=rmw_fastrtps_cpp\n'
                + 'export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\n'
                + f'export FASTRTPS_DEFAULT_PROFILES_FILE={remote_bundle}/configs/fastdds-udp-only.xml\n'
            )
        if selected == 'production-gateway':
            return (
                unset_line
                + 'source /opt/ros/jazzy/setup.bash || exit\n'
                + 'export RMW_IMPLEMENTATION=rmw_fastrtps_cpp\n'
            )
        if selected == 'cyclone':
            return (
                unset_line
                + 'source /opt/ros/jazzy/setup.bash || exit\n'
                + 'source /home/user/workspace/f1/install/setup.bash || exit\n'
                + 'source /home/user/workspace/agv/install/setup.bash || exit\n'
                + 'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp\n'
                + 'export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml\n'
                + 'export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\n'
                + 'unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS\n'
            )
        raise ValueError('unsupported production_ros_env: ' + str(selected))
    if ros_env == 'cyclone':
        return (
            'unset RMW_IMPLEMENTATION FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE '
            'CYCLONEDDS_URI ROS_LOCALHOST_ONLY ROS_STATIC_PEERS\n'
            'source /opt/ros/jazzy/setup.bash || exit\n'
            'source /home/user/workspace/f1/install/setup.bash || exit\n'
            'source /home/user/workspace/agv/install/setup.bash || exit\n'
            'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp\n'
            'export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml\n'
            'export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\n'
        )
    if ros_env == 'fastdds-udp':
        return (
            'unset RMW_IMPLEMENTATION FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE '
            'CYCLONEDDS_URI ROS_LOCALHOST_ONLY ROS_STATIC_PEERS\n'
            'source /opt/ros/jazzy/setup.bash || exit\n'
            'export RMW_IMPLEMENTATION=rmw_fastrtps_cpp\n'
            'export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\n'
            'FASTDDS_UDP_XML=$(mktemp /tmp/f1-preflight-fastdds-XXXXXX.xml)\n'
            'trap "rm -f $FASTDDS_UDP_XML" EXIT\n'
            'cat > "$FASTDDS_UDP_XML" <<\'XML\'\n'
            + FASTDDS_UDP_ONLY_XML
            + 'XML\n'
            'export FASTRTPS_DEFAULT_PROFILES_FILE="$FASTDDS_UDP_XML"\n'
        )
    raise ValueError('unsupported ros_env: ' + ros_env)


def _run_remote_orchestrator(args: argparse.Namespace) -> int:
    """Orchestrate the read-only preflight from 6000D over SSH without authorizing motion."""
    ws_cfg_path = args.workspace_config.resolve()
    ws = _read_json(ws_cfg_path)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)

    manifest_path = Path(ws['root']) / 'manifest.json'
    manifest = _read_json(manifest_path)
    bundle_id = manifest['bundle_id']
    short = bundle_id[:12]
    remote_bundle = args.remote_bundle or ws['remote_bundle']
    remote_site = args.remote_site or ('/home/user/Bruno/HARNESS/site/f1-llm-' + short)
    remote_out = f'/tmp/f1-preflight-scene-{short}-{int(time.time())}-{os.getpid()}'

    # Verify local SAM is healthy before SSHing to ARM
    local_sam = args.local_sam_url.rstrip('/')
    with urlopen(local_sam + '/health', timeout=5) as resp:
        local_sam_health = json.load(resp)
    if local_sam_health.get('ready') is not True:
        raise RuntimeError('local SAM service is not ready: ' + json.dumps(local_sam_health))
    local_sam_port = urlsplit(local_sam).port or 19842

    common = [
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=15',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=' + ws['known_hosts'],
        '-i', ws['identity'],
    ]
    ssh_base = ['ssh', *common, '-p', str(ws['port']), ws['host']]
    scp_base = ['scp', '-q', *common, '-P', str(ws['port'])]

    # Ship this script to a temporary path on ARM so the command works even before
    # a bundle includes preflight_scene.py (or uses the bundle copy if --use-bundle-script).
    remote_script = (remote_bundle + '/scripts/preflight_scene.py') if args.use_bundle_script else (remote_out + '.py')
    if not args.use_bundle_script:
        subprocess.run(scp_base + [str(Path(__file__).resolve()), f"{ws['host']}:{remote_script}"], check=True, timeout=30)

    remote_sam_url = f'http://127.0.0.1:{args.remote_sam_port}'
    python_bin = '/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python'
    worker_cmd = [
        python_bin,
        remote_script,
        '--local',
        '--root', remote_bundle,
        '--site', remote_site,
        '--output', remote_out,
        '--bundle-id', bundle_id,
        '--seconds', str(args.seconds),
        '--acquire-timeout', str(args.acquire_timeout),
        '--max-left-gap-s', str(args.max_left_gap_s),
        '--sam-url', remote_sam_url,
    ]
    bash_script = (
        _build_remote_ros_prelude(args.ros_env, remote_bundle)
        + 'exec ' + shlex.join(worker_cmd) + '\n'
    )
    ssh_cmd = [
        'ssh',
        *common,
        '-o', 'ExitOnForwardFailure=yes',
        '-R', f'127.0.0.1:{args.remote_sam_port}:127.0.0.1:{local_sam_port}',
        '-p', str(ws['port']),
        ws['host'],
        shlex.join(['bash', '-c', bash_script]),
    ]
    proc = subprocess.run(ssh_cmd, text=True, capture_output=True, timeout=args.acquire_timeout + 120)
    (out / 'remote-stdout.txt').write_text(proc.stdout, encoding='utf-8')
    (out / 'remote-stderr.txt').write_text(proc.stderr, encoding='utf-8')

    # Fetch result.json (and service logs if present) back to 6000D
    fetch_ok = False
    try:
        subprocess.run(scp_base + [f"{ws['host']}:{remote_out}/result.json", str(out / 'result.json')], check=True, timeout=30)
        fetch_ok = True
        for log_name in ('gateway.log', 'numerics.log', 'verify.log'):
            subprocess.run(
                scp_base + [f"{ws['host']}:{remote_out}/{log_name}", str(out / log_name)],
                check=False,
                timeout=20,
                capture_output=True,
            )
    finally:
        cleanup = ['rm', '-rf', remote_out]
        if not args.use_bundle_script:
            cleanup.append(remote_script)
        subprocess.run(ssh_base + [shlex.join(cleanup)], check=False, timeout=20, capture_output=True)

    if fetch_ok:
        report = _read_json(out / 'result.json')
        print(format_report(report), flush=True)
        return 0 if report.get('passed') else 2

    print(proc.stdout, end='')
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end='')
    return proc.returncode or 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--local', action='store_true', help='Run directly on ARM against local bundle/site')
    parser.add_argument('--workspace-config', type=Path, default=Path(__file__).resolve().parents[1] / 'site/workspace.local.json')
    parser.add_argument('--root', type=Path, help='Frozen bundle directory (--local mode)')
    parser.add_argument('--site', type=Path, help='Site directory (--local mode)')
    parser.add_argument('--remote-bundle', help='Override remote bundle path (remote mode)')
    parser.add_argument('--remote-site', help='Override remote site path (remote mode)')
    parser.add_argument('--bundle-id', help='Expected bundle_id')
    parser.add_argument('--profile', type=Path, help='Override robot profile path (--local mode)')
    parser.add_argument('--output', type=Path, required=True, help='Output directory for preflight receipt')
    parser.add_argument('--seconds', type=float, default=12.0, help='Required continuous paired window (s), >= 10')
    parser.add_argument('--acquire-timeout', type=float, default=48.0, help='Maximum acquisition duration (s)')
    parser.add_argument('--max-left-gap-s', type=float, default=2.0, help='Max allowed gap between left-wrist frames (s)')
    parser.add_argument('--ros-env', choices=('production-gateway', 'cyclone', 'fastdds-udp'), default='production-gateway')
    parser.add_argument('--sam-url', help='Override loopback SAM URL (--local mode)')
    parser.add_argument('--local-sam-url', default='http://127.0.0.1:19842', help='Local SAM URL on 6000D (remote mode)')
    parser.add_argument('--remote-sam-port', type=int, default=19852, help='Temporary loopback SAM port forwarded on ARM')
    parser.add_argument('--use-bundle-script', action='store_true', help='Run preflight_scene.py from remote bundle instead of uploading')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.local:
        if args.root is None or args.site is None:
            parser.error('--root and --site are required with --local')
        return _run_local_worker(args)
    return _run_remote_orchestrator(args)


if __name__ == '__main__':
    raise SystemExit(main())
