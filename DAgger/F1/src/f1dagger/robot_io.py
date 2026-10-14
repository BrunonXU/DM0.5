"""Sole F1 command owner, using F1-infra ROS and approved output primitives."""
import fcntl
import json
import os
from pathlib import Path
import time
import numpy as np
from .config import infra_import, pico_paths
from .contract import ARM_NAMES, FULL_NAMES, MODEL_NAMES, vector, sha256

KEYS = ('joint_command', 'left_gripper_command', 'right_gripper_command')


class ControlLock:
    def __init__(self, path):
        if not path:
            raise ValueError('F1_CONTROL_LOCK must be the same path as F1-infra/Pico')
        self.path, self.handle = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise RuntimeError('F1 control lock occupied; end the existing local control session')
        self.handle = handle
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(dict(owner='f1dagger', pid=os.getpid())))
        handle.flush()

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class CollisionGate:
    def __init__(self, config, mapping, runtime, full22):
        import yaml
        import mujoco
        if mujoco.__version__ != '3.3.7':
            raise RuntimeError('selected F1 collision backend requires mujoco==3.3.7')
        root = Path(config['_infra_root'])
        upper = pico_paths(root)
        os.environ['PICO_F1_ROOT'] = str(upper)
        os.environ['PICO_UPPER_RUNTIME'] = str(runtime)
        os.environ['PICO_UPPER_RELEASE_HASH'] = config['_infra_manifest_sha256'] + '-' + config['_profile_sha256']
        template = upper/'vendor/f1_safety/src/python/f1_safety_config.yaml'
        cfg = yaml.safe_load(template.read_text())
        model = root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
        if sha256(model) != json.loads((root/'configs/model_mapping.json').read_text())['model_urdf_sha256']:
            raise ValueError('collision and Pink model assets differ')
        # Collision checker consumes model radians; authoritative command limits
        # are enforced separately by JointMapping.check_action in HAL degrees.
        runtime = Path(runtime)
        runtime.mkdir(parents=True, exist_ok=True)
        # The legacy converter resolves meshes from URDF.parents[2], whereas
        # the current workbench URDF uses paths relative to the URDF itself.
        # Stage unchanged kinematics with package mesh paths in run-local cache.
        from xml.etree import ElementTree as ET
        tree = ET.parse(model)
        staged = runtime/'model_input/f1_wheel/urdf'/model.name
        staged.parent.mkdir(parents=True, exist_ok=True)
        meshes = staged.parent.parent/'meshes'
        meshes.mkdir(exist_ok=True)
        for element in tree.findall('.//mesh'):
            original = (model.parent/element.attrib['filename']).resolve(strict=True)
            if root not in original.parents:
                raise ValueError('mesh outside bound F1 assets')
            link = meshes/original.name
            if not link.exists():
                import shutil
                shutil.copyfile(original, link)
            element.set('filename', 'package://f1_wheel/meshes/'+original.name)
        tree.write(staged, encoding='utf-8', xml_declaration=True)
        cfg['model']['urdf_path'] = str(staged)
        # Feedback and command vectors already use the current canonical model
        # coordinates; body_j1..5 map to body_joint5..1 in the F1 joint contract.
        cfg['joint_map'] = dict(zip(FULL_NAMES, MODEL_NAMES))
        (runtime/'model_binding.json').write_text(json.dumps(dict(
            source_urdf_sha256=sha256(model), staged_urdf_sha256=sha256(staged),
            transform='mesh_paths_only', joint_map=cfg['joint_map']), indent=2))
        config_path = runtime/'collision.yaml'
        config_path.write_text(yaml.safe_dump(cfg))
        from com_and_collision_protect_modules import SafetyProtector
        self.protector = SafetyProtector(config_path=str(config_path), use_com_guard=True,
                                          use_collision_guard=True, verbose=False)
        if self.protector._collision_guard is None or self.protector._com_guard is None:
            raise RuntimeError('F1 collision/CoM modules failed to initialize')
        self.mapping = mapping
        self.protector.init_safe_cmd(np.r_[mapping.to_model(full22), 0., 0.])
        initial = np.r_[vector(full22, 22)[:14], 0., 0.]
        self.check(initial, full22)

    def check(self, action, full22):
        target = vector(full22, 22)
        target[:14] = vector(action, 16)[:14]
        model = self.mapping.to_model(target)
        self.protector.set_feedback(dict(zip(FULL_NAMES, self.mapping.to_model(full22))))
        safe, indices, _unused_model_fallback, flags = self.protector.run(np.r_[model, 0., 0.])
        if not safe:
            raise RuntimeError('F1 collision/CoM rejected target: ' + self.protector.last_reason)
        return dict(safe=True, arm_collision_indices=list(indices))


def make_output(config):
    upper = pico_paths(config['_infra_root'])
    from pico_f1.config import arm_output_limits
    from pico_f1.arm_output import ApprovedArmOutput
    controls = json.loads((upper/'config/control.json').read_text())
    if controls['command_hz'] != 50 or controls['collision_check_hz'] != 50:
        raise ValueError('F1 output profile differs from the 50/250 Hz adapter')
    from .arm_output import RadianArmTrajectory
    output = ApprovedArmOutput(**arm_output_limits(controls))
    output.planner = RadianArmTrajectory(output.planner)
    return output


class F1Owner:
    def __init__(self, transport, source, mapping, output, gate, *, powered=False):
        self.transport, self.source, self.mapping = transport, source, mapping
        self.output, self.gate, self.powered = output, gate, powered
        if callable(getattr(getattr(output, 'planner', None), 'set_limits', None)):
            output.planner.set_limits(mapping.limits)
        self.last_submitted = None
        self.selected = None
        self.prepared = False
        self.sequence = 0
        self.last_evidence = {}
        self.updated = None
        self.field_times = [None] * 16
        self.gripper_sent = [False, False]

    def initialize(self, now):
        full, state, _ = self.source.feedback()
        self.last_submitted = self.mapping.check_action(state)
        self.selected = self.last_submitted.copy()
        self.output.reset()
        self.output.push(state[:14], now, enabled=(False, False))
        self.updated = now
        return full

    def prepare(self):
        if self.powered and not self.prepared:
            try:
                for key in KEYS:
                    self.transport.prepare(key)
            except BaseException:
                self.close()
                raise
            self.prepared = True

    def hold(self, now):
        """Discard the target, filtering state and velocity, retaining last publication."""
        if self.last_submitted is not None:
            self.output.reset()
            self.output.push(self.last_submitted[:14], now, enabled=(False, False))
            self.selected = self.last_submitted.copy()
            self.updated = now

    def approve(self, target, now, *, enabled=(True, True)):
        full, _, _ = self.source.feedback()
        target = self.mapping.check_action(target)
        self.gate.check(target, full)
        self.output.push(target[:14], now, enabled=enabled)
        self.selected, self.updated = target, now

    def _output_snapshot(self, now):
        """Preserve the input state before sample/hold can clear a fault."""
        output = self.output
        planner = getattr(output, 'planner', None)
        sampled, updated = getattr(output, 'sampled', None), getattr(output, 'updated', None)
        snapshot = dict(sample_time_s=now, previous_sample_time_s=sampled,
                        target_updated_time_s=updated,
                        output_gap_ms=None if sampled is None else (now-sampled)*1000.,
                        target_age_ms=None if updated is None else (now-updated)*1000.,
                        owner_target_age_ms=None if self.updated is None else (now-self.updated)*1000.)
        for key in ('position', 'target', 'enabled'):
            value = getattr(output, key, None)
            snapshot[key] = None if value is None else np.asarray(value).tolist()
        if planner is not None:
            snapshot['limit_control'] = getattr(planner, 'limit_diagnostics', None)
            snapshot['planner_units'] = 'degree'
            snapshot['planner_internal_units'] = getattr(planner, 'internal_units', 'degree')
            snapshot['planner'] = {
                key: np.asarray(getattr(planner, key)).tolist()
                for key in ('position', 'velocity', 'acceleration', 'filtered_target',
                            'max_velocity', 'max_acceleration', 'max_jerk', 'recovery_events')
                if hasattr(planner, key)
            }
        return snapshot

    def publish(self, now):
        # Reset before any fallible check: a failure must never inherit the
        # previous successful call's evidence. Partial topic writes remain
        # visible even when there is no complete 16D control record.
        started = time.monotonic_ns()
        evidence = dict(publication_complete=False, publications=[],
                        execution_mode='powered' if self.powered else 'shadow',
                        started_monotonic_ns=started,
                        previous_command_sequence=self.sequence,
                        stage='feedback')
        self.last_evidence = evidence
        full, measured, _ = self.source.feedback()
        evidence['stage'] = 'target_validation'
        if self.updated is None or not 0 <= now-self.updated <= .15:
            raise RuntimeError('approved target stale')
        # sample() holds the planner on failure, clearing its velocity and
        # acceleration. Retain pre-sample state only in fault evidence.
        output_snapshot = self._output_snapshot(now)
        arms = self.output.sample(now)
        evidence['limit_control'] = getattr(getattr(self.output, 'planner', None), 'limit_diagnostics', None)
        if self.output.fault:
            fault = str(self.output.fault)
            evidence.update(stage='output_sample', output_fault=fault,
                            output_diagnostics=output_snapshot)
            output_snapshot['timed_out'] = bool(getattr(self.output, 'timed_out', False))
            self.hold(now)
            raise RuntimeError('F1 output fault latched: ' + fault)
        command = self.mapping.check_action(np.r_[arms, self.selected[14:]])
        # Runtime invokes this on a single owner thread. This is publication
        # evidence, not a hardware acknowledgement or measured arrival.
        evidence['attempted_command'] = command.tolist()
        if self.powered:
            evidence['stage'] = 'endpoint_check'
            if not self.prepared:
                raise RuntimeError('command endpoints are not explicitly prepared')
            values = (dict(zip(ARM_NAMES, command[:14])), float(command[14]), float(command[15]))
            for index, (key, value) in enumerate(zip(KEYS, values)):
                evidence['stage'] = 'exclusive_check'
                evidence['topic_key'] = key
                self.transport.assert_exclusive_cached(key)
                if index and self.gripper_sent[index-1] and command[13+index] == self.last_submitted[13+index]:
                    continue  # Keep the real same-owner gripper command and its original time.
                evidence['stage'] = 'publish'
                self.transport.publish(key, value)
                stamp = time.monotonic_ns()
                if index == 0:
                    self.field_times[:14] = [stamp] * 14
                else:
                    self.field_times[13+index] = stamp
                    self.gripper_sent[index-1] = True
                gid = self.transport.rmw_gids.get(key) or self.transport.own_gids.get(key)
                evidence['publications'].append(dict(topic_key=key, monotonic_ns=stamp, publisher_gid=gid))
                evidence['action_source_timestamps_ns'] = list(self.field_times)
            evidence['publication_complete'] = True
        self.sequence += 1
        self.last_submitted = command.copy()
        evidence.pop('topic_key', None)
        evidence.update(stage='completed', command_sequence=self.sequence, started_monotonic_ns=started,
                        completed_monotonic_ns=time.monotonic_ns())
        return command, measured, evidence

    def close(self):
        for key in KEYS:
            self.transport.release(key)
        self.prepared = False
