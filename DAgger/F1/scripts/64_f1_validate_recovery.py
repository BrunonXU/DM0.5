#!/usr/bin/env python3
"""Validate captured F1 boundary/output recovery numerically; no ROS or actions."""
import argparse
import contextlib
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
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

from f1dagger.config import DAGGER_ROOT, load_config, pico_paths
from f1dagger.contract import JointMapping, FULL_NAMES, sha256
from f1dagger.teleop import F1ArmTarget, ArmBoundaryHold
from f1dagger.robot_io import make_output


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_dynamics(output, before, dt, maxima):
    p = output.planner
    values = dict(velocity=float(np.max(np.abs(p.velocity))),
                  acceleration=float(np.max(np.abs(p.acceleration))),
                  jerk=float(np.max(np.abs(np.asarray(p.acceleration)-before))/dt))
    for key, maximum in values.items():
        maxima[key] = max(maxima.get(key, 0.), maximum)
        limit = getattr(p, 'max_'+key)
        require(maximum <= limit + (1e-5 if key == 'jerk' else 1e-7), key+' bound exceeded')


def boundary_checks(c, mapping, urdf, full, cases):
    results = []
    def intent(side, axes=(0., 0., 0.), angular=(0., 0., 0.)):
        return SimpleNamespace(active_side=side, mode='combined', axes=axes,
                               angular_axes=angular, speed_scale=1.)
    for number, (side, command_arm, measured_arm, axes, angular) in enumerate(cases, 1):
        feedback = full.copy()
        offset = 0 if side == 'left' else 7
        feedback[offset:offset+7] = measured_arm
        original_feedback = feedback.copy()
        command = np.r_[feedback[:14], 43., 66.]
        command[offset:offset+7] = command_arm
        arm = F1ArmTarget(urdf, mapping, c['ik'])
        for step in range(500):
            command = arm.target(intent(side, axes, angular), feedback, command, .02)
            mapping.check_action(command)
            pose = arm.ik.configuration.get_transform_frame_to_world(arm.ik.frame)
            np.testing.assert_allclose(arm.ik.target_pose.homogeneous,pose.homogeneous,atol=1e-12)
        boundary = dict(continuous=True, constraints=arm.ik.limit_diagnostics)
        stopped = command.copy()
        for _ in range(25):
            command = arm.target(intent(side, [-v for v in axes], [-v for v in angular]),
                                 feedback, command, .02)
            mapping.check_action(command)
        reverse_delta = float(command[offset+1]-stopped[offset+1])
        require(reverse_delta > .01, 'reverse input did not move J2 away from boundary')
        other = slice(7, 14) if side == 'left' else slice(0, 7)
        np.testing.assert_array_equal(command[other], stopped[other])
        np.testing.assert_array_equal(command[14:], stopped[14:])
        np.testing.assert_array_equal(feedback, original_feedback)
        results.append(dict(case=number, side=side, outward_steps=step+1,
                            boundary=boundary, reverse_j2_delta_deg=reverse_delta,
                            release_called_for_recovery=False, feedback_unchanged=True))
    return results


def original_fault_checks(data):
    import ruckig
    results = []
    for synchronization in (ruckig.Synchronization.Time, ruckig.Synchronization.No):
        for dt in (.00063, .004, .0135):
            inp, out = ruckig.InputParameter(14), ruckig.OutputParameter(14)
            for key, value in data.items():
                setattr(inp, key, value)
            inp.synchronization = synchronization
            try:
                ruckig.Ruckig(14, dt).update(inp, out)
            except Exception as exc:
                error = str(exc).split('input:')[0].strip()
                require('error in step 1, dof: 2' in error, 'different original degree fault: '+error)
            else:
                raise ValueError('captured original degree fault did not reproduce on this machine')
            results.append(dict(dt_s=dt, synchronization=str(synchronization), error=error))
    return results


def output_recovery_checks(c, cases):
    data = cases['input_degrees']
    results = []
    for intervals in ((.004,), (.00063,), (.0135,), (.00063, .004, .0135, .001)):
        output = make_output(c)
        output.push(data['current_position'], 0., enabled=(True, True))
        native = output.planner._radians
        for key in ('position', 'velocity', 'acceleration'):
            setattr(native, key, np.deg2rad(data['current_'+key]).tolist())
        native.filtered_target = np.deg2rad(data['target_position']).tolist()
        now, steps, maxima = 0., 0, {}
        while now < 1.:
            dt = intervals[steps % len(intervals)]
            before = np.array(output.planner.acceleration)
            now += dt
            output.push(data['target_position'], now, enabled=(True, True))
            command = output.sample(now)
            require(output.fault is None, str(output.fault))
            check_dynamics(output, before, dt, maxima)
            steps += 1
        error = float(np.max(np.abs(command-data['target_position'])))
        require(error <= output.planner.deadband, 'output did not settle within existing deadband')
        require(np.max(np.abs(output.planner.velocity)) < 1e-5, 'output velocity did not settle')
        require(np.max(np.abs(output.planner.acceleration)) < 1e-5, 'output acceleration did not settle')
        results.append(dict(intervals_s=intervals, steps=steps, maxima_degrees=maxima,
            goal_error_deg=error, existing_deadband_deg=output.planner.deadband,
            time_to_independent_retries=output.planner.recovery_events))
    return results


def home_checks(c, cases):
    output = make_output(c)
    output.push(cases['home_initial_degrees'], 0., enabled=(False, False))
    now, maxima = 0., {}
    steps = cases['home_steps'] + [[.004, cases['home_steps'][-1][1]]] * 500
    for dt, target in steps:
        before = np.array(output.planner.acceleration)
        now += dt
        output.push(target, now, enabled=(True, True))
        command = output.sample(now)
        require(output.fault is None, str(output.fault))
        check_dynamics(output, before, dt, maxima)
    error = float(np.max(np.abs(command-steps[-1][1])))
    require(error <= output.planner.deadband, 'Home replay failed to settle')
    return dict(recorded_steps=len(cases['home_steps']), settling_steps=500,
                maxima_degrees=maxima, goal_error_deg=error,
                existing_deadband_deg=output.planner.deadband)


def protection_checks(c):
    output = make_output(c)
    initial = np.arange(14.)
    output.push(initial, 0., enabled=(True, True))
    held = output.sample(.2)
    require(bool(output.fault) and output.timed_out, 'output watchdog did not latch')
    reason = output.fault
    for index in range(1, 101):
        now = .2+index*.004
        output.push(initial+30., now, enabled=(True, True))
        np.testing.assert_array_equal(output.sample(now), held)
    require(bool(output.fault), 'active targets cleared the output fault')
    return dict(watchdog_fault=reason, active_target_rearmed=False, held_position_unchanged=True)


def continuous_checks(c, mapping, urdf, full):
    import time
    result = {'ik': [], 'output': []}
    for side in ('left','right'):
        for axis in range(3):
            command = np.r_[full[:14],43.,66.]
            arm = F1ArmTarget(urdf,mapping,c['ik'])
            direction = -np.eye(3)[axis]
            timings = []
            for tick in range(520):
                axes = direction if tick < 500 else -direction
                packet = SimpleNamespace(active_side=side,mode='combined',axes=axes,
                    angular_axes=(0.,0.,0.),speed_scale=1.)
                before = None if arm.ik is None else arm.ik.target_pose.translation.copy()
                started=time.perf_counter()
                command=arm.target(packet,full,command,.02)
                timings.append((time.perf_counter()-started)*1000.)
                mapping.check_action(command)
                if tick >= 500:
                    require(np.dot(arm.ik.target_pose.translation-before,direction)<-1e-9,
                            'direct reverse did not produce inward increment')
            result['ik'].append(dict(side=side,axis=axis,outward_s=10.,
                first_reverse_tick_ms=20,rb_release_required=False,
                solve_ms_p99=float(np.percentile(timings,99)),solve_ms_max=max(timings)))
    for wrist in (False,True,'tangent'):
        output=make_output(c);p=output.planner;p.set_limits(mapping.limits)
        initial=full[:14].copy()
        if wrist == 'tangent':
            initial[5:7]=[40.,69.5-1e-4]
            target=initial.copy();target[5:7]=[60.,49.-1e-4]
        elif wrist:
            initial[5:7]=[40.,50.]
            target=initial.copy();target[5:7]=[55.,110.5-1.025*55.-1e-8]
        else:
            initial[1]=-90.;target=initial.copy();target[1]=-94.+1e-8
        output.push(initial,0.,enabled=(True,False))
        now=0.;maxima={};timings=[];modes={};phase_count=0
        for tick in range(2400):
            dt=(.00063,.004,.0135,.005)[tick%4];now+=dt
            output.push(target if tick<1200 else initial,now,enabled=(True,False))
            before=np.array(p.acceleration);started=time.perf_counter()
            command=output.sample(now);timings.append((time.perf_counter()-started)*1000.)
            require(output.fault is None,'bounded output fault: '+str(output.fault))
            mapping.check_action(np.r_[command,43.,66.])
            check_dynamics(output,before,dt,maxima)
            mode=p.limit_diagnostics['mode'];modes[mode]=modes.get(mode,0)+1
            phase_count += p.limit_diagnostics.get('synchronization')=='phase'
        np.testing.assert_allclose(command,initial,atol=.002,rtol=0)
        if wrist == 'tangent': require(phase_count>0,'tangent did not exercise phase sync')
        result['output'].append(dict(wrist=wrist,phase_count=phase_count,steps=2400,maxima_degrees=maxima,modes=modes,
            sample_ms_p99=float(np.percentile(timings,99)),sample_ms_max=max(timings),
            reverse_settled=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--infra-root', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--config', default=str(DAGGER_ROOT/'configs/f1_09_hil.json'))
    parser.add_argument('--runtime', required=True, type=Path)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if runtime.exists():
        parser.error('use a new validation runtime; existing evidence is immutable')
    runtime.mkdir(parents=True)
    report = dict(ok=False, robot_control_started=False,
        scope='captured_state_numerics_only_not_live_shadow_or_motion',
        checked_at_utc=datetime.now(timezone.utc).isoformat(), architecture=platform.machine(), checks={})
    try:
        c, profile = load_config(args.config, infra_root=args.infra_root, profile_path=args.profile)
        root = Path(c['_infra_root'])
        urdf = root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
        fixture = Path(__file__).with_name('64_f1_recovery_cases.json')
        cases = json.loads(fixture.read_text())
        mapping = JointMapping(json.loads((root/'configs/model_mapping.json').read_text()), profile, urdf)
        preset = next(p for p in profile['default_presets'] if p['key'] == 'native-pose2-full-v1')
        full = np.array([preset['joints'][name] for name in FULL_NAMES])
        infra = pico_paths(root)
        files = [Path(__file__), fixture, DAGGER_ROOT/'src/f1dagger/arm_output.py',
                 DAGGER_ROOT/'src/f1dagger/robot_io.py', DAGGER_ROOT/'src/f1dagger/teleop.py',
                 DAGGER_ROOT/'src/f1dagger/constrained_ik.py', DAGGER_ROOT/'src/f1dagger/trajectory_bounds.py',
                 Path(__import__('dagger_core.pink_ik',fromlist=['__file__']).__file__), Path(__import__('dagger_core.powered_teleop',fromlist=['__file__']).__file__),
                 infra/'src/pico_f1/arm_output.py', infra/'src/pico_f1/trajectory.py',
                 infra/'config/control.json', root/'configs/model_mapping.json', urdf]
        report.update(infra_binding=c['_infra_manifest_sha256'], config_sha256=c['_config_sha256'],
            profile_sha256=c['_profile_sha256'], inputs={str(path):sha256(path) for path in files},
            captured_sources={key:cases[key] for key in ('source_episode', 'source_console_sha256',
                'source_control_sha256', 'boundary_source', 'sample_dt_note')},
            versions={name:importlib.metadata.version(name) for name in ('numpy', 'ruckig', 'pin', 'pin-pink')})
        checks = report['checks']
        with (runtime/'numerics.log').open('x') as log, contextlib.redirect_stdout(log):
            checks['joint_boundaries'] = boundary_checks(c, mapping, urdf, full, cases['boundary_cases'])
            checks['original_degree_fault'] = original_fault_checks(cases['input_degrees'])
            checks['radian_output_recovery'] = output_recovery_checks(c, cases)
            checks['recorded_home_replay'] = home_checks(c, cases)
            checks['watchdog_unchanged'] = protection_checks(c)
            checks['continuous_limits'] = continuous_checks(c,mapping,urdf,full)
        require('rclpy' not in sys.modules and 'pytest' not in sys.modules, 'validation imported ROS or pytest')
        report.update(ok=True, ros_imported=False, pytest_imported=False)
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (runtime/'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(ok=report['ok'], report=str(runtime/'report.json'),
                         robot_control_started=False, architecture=report['architecture'])))


if __name__ == '__main__':
    main()
