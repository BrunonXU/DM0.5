#!/usr/bin/env python3
"""Coupled IK/output numerical regression: no ROS, sockets or robot actions."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import time
from types import SimpleNamespace
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from f1dagger.config import DAGGER_ROOT, load_config
from f1dagger.contract import JointMapping, sha256
from f1dagger.teleop import F1ArmTarget
from f1dagger.robot_io import make_output


def run_case(c, mapping, urdf, full, side='right', axis=0, sign=-1, jitter=False, irregular=False):
    arm = F1ArmTarget(urdf, mapping, c['ik'])
    output = make_output(c); output.planner.set_limits(mapping.limits)
    command = np.r_[full[:14], 43., 66.]
    offset = 0 if side == 'left' else 7
    sl = slice(offset, offset+7)
    enabled = (side == 'left', side == 'right')
    arm.begin(side, full, command)
    output.push(command[:14], 0., enabled=enabled)
    now = 0.; next_ik = 0.; previous_ik = 0.; tick = 0
    rows = []; timings = []; modes = Counter()
    max_jump = 0.; max_lead = 0.; excess_increment = 0.
    dynamics = dict(velocity=0., acceleration=0., jerk=0.)
    first_reverse_increment = None
    initial_full = full.copy()
    while now < 14.:
        dt = (.002, .004, .006, .004)[tick%4] if irregular else .004
        now += dt
        if now+1e-10 >= next_ik:
            ik_dt = .02 if not previous_ik else now-previous_ik
            previous_ik = now; next_ik = now+.02
            direction = sign*np.eye(3)[axis]
            axes = direction.copy() if now < 10. else -direction if now < 12. else np.zeros(3)
            if jitter and now < 10.:
                axes[(axis+1)%3] = .005*(1 if int(now/.1)%2 else -1)
            packet = SimpleNamespace(active_side=side, mode='combined', axes=axes,
                                     angular_axes=(0.,0.,0.), speed_scale=2.)
            prior = arm.ik_virtual_q.copy()
            published = np.deg2rad(command[sl]*mapping.signs[sl])
            old_pose = arm.ik.configuration.get_transform_frame_to_world(arm.ik.frame).translation.copy()
            started = time.perf_counter()
            target = arm.target(packet, full, command, ik_dt)
            timings.append((time.perf_counter()-started)*1000.)
            candidate = arm.ik_virtual_q.copy()
            max_jump = max(max_jump, float(np.max(np.abs(candidate-prior))))
            lead = np.abs(candidate-published)
            max_lead = max(max_lead, float(np.max(lead)))
            excess = lead-np.maximum(arm.max_otg_ik_lag_rad, np.abs(prior-published))
            excess_increment = max(excess_increment, float(np.max(excess)))
            assert np.max(np.abs(candidate-prior)) <= c['ik']['safety']['max_joint_step_rad']+1e-9
            assert np.max(excess) <= 1e-9, 'new IK increment accumulated out-of-window lead'
            if now >= 10. and first_reverse_increment is None:
                new_pose = arm.ik.configuration.get_transform_frame_to_world(arm.ik.frame).translation
                first_reverse_increment = float(np.dot(new_pose-old_pose, direction))
            output.push(target[:14], now, enabled=enabled)
        old_a = np.asarray(output.planner.acceleration)
        command[:14] = output.sample(now)
        assert output.fault is None, str(output.fault)
        mapping.check_action(command)
        p = output.planner
        for key, value in dict(velocity=np.max(np.abs(p.velocity)),
                              acceleration=np.max(np.abs(p.acceleration)),
                              jerk=np.max(np.abs(np.asarray(p.acceleration)-old_a))/dt).items():
            dynamics[key] = max(dynamics[key], float(value))
            assert value <= getattr(p, 'max_'+key)+1e-5, key
        modes[p.limit_diagnostics['mode']] += 1
        # Use a separate configuration so diagnostics never mutate solver state.
        cfg = arm.ik.pink.Configuration(arm.ik.model, arm.ik.model.createData(),
                                       np.deg2rad(command[sl]*mapping.signs[sl]))
        xyz = cfg.get_transform_frame_to_world(arm.ik.frame).translation
        rows.append([now, float(sign*xyz[axis]), float(np.max(np.abs(p.velocity)))])
        tick += 1
    rows = np.asarray(rows)
    speed = np.diff(rows[:,1])/np.diff(rows[:,0])
    window = speed[(rows[1:,0]>=1.) & (rows[1:,0]<2.)]*1000.
    quantiles = np.quantile(window, [0,.5,1])
    # These are the captured-pose cases that exposed v14-r2's periodic dips.
    if side == 'right' and sign == -1 and axis in (0,2):
        assert quantiles[0] > .85*quantiles[1], ('steady speed valley', quantiles)
        assert quantiles[2] < 1.2*quantiles[1], ('steady speed spike', quantiles)
    assert first_reverse_increment < 0., 'first reverse candidate did not move inward'
    assert np.mean(speed[(rows[1:,0]>=11.) & (rows[1:,0]<12.)]) < 0, 'reverse did not recover'
    assert rows[-1,2] < .01, 'centered stick did not settle'
    np.testing.assert_array_equal(full, initial_full)
    np.testing.assert_array_equal(command[14:], [43.,66.])
    other = slice(7,14) if side == 'left' else slice(0,7)
    np.testing.assert_array_equal(command[other], full[other])
    return dict(side=side, axis=axis, sign=sign, jitter=jitter, irregular=irregular,
                steps=tick, speed_1_2s_min_median_max_mm_s=quantiles.tolist(),
                max_target_step_rad=max_jump, max_publication_lead_rad=max_lead,
                max_added_excess_lead_rad=excess_increment,
                first_reverse_candidate_projection_m=first_reverse_increment,
                dynamics_degrees=dynamics, modes=dict(modes),
                solve_ms_p99=float(np.percentile(timings,99)), solve_ms_max=max(timings),
                reverse_recovered=True, centered_settled=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--infra-root', required=True); p.add_argument('--profile', required=True)
    p.add_argument('--runtime', required=True, type=Path)
    args = p.parse_args()
    args.runtime.mkdir(parents=True, exist_ok=False)
    report = dict(ok=False, robot_control_started=False, architecture=platform.machine(),
                  checked_at_utc=datetime.now(timezone.utc).isoformat(), cases=[])
    try:
        c, profile = load_config(DAGGER_ROOT/'configs/f1_09_hil.json',
                                infra_root=args.infra_root, profile_path=args.profile)
        root = Path(c['_infra_root']); urdf=root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
        fixture = Path(__file__).with_suffix('.json')
        captured = json.loads(fixture.read_text()); full=np.asarray(captured['full22'],dtype=float)
        mapping = JointMapping(json.loads((root/'configs/model_mapping.json').read_text()),profile,urdf)
        files = [Path(__file__), fixture, urdf]+[DAGGER_ROOT/'src/f1dagger'/name for name in
                 ('teleop.py','constrained_ik.py','arm_output.py','trajectory_bounds.py','robot_io.py')]
        report.update(inputs={str(f):sha256(f) for f in files}, captured_source=captured,
                      config_sha256=c['_config_sha256'], profile_sha256=c['_profile_sha256'],
                      infra_binding=c['_infra_manifest_sha256'])
        for side in ('left','right'):
            for axis in range(3):
                report['cases'].append(run_case(c,mapping,urdf,full,side,axis))
        for kwargs in (dict(sign=1), dict(jitter=True),dict(irregular=True)):
            report['cases'].append(run_case(c,mapping,urdf,full,**kwargs))
        assert 'rclpy' not in sys.modules
        report.update(ok=True, ros_imported=False)
    except BaseException as exc:
        report['error']=repr(exc)
        raise
    finally:
        (args.runtime/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(ok=True,report=str(args.runtime/'report.json'),cases=len(report['cases']))))

if __name__ == '__main__': main()
