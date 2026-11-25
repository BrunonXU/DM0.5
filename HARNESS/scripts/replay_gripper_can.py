"""Offline plant adapter for the same published-close CAN evidence used by the gateway."""
from collections import deque
from types import SimpleNamespace

from f1_harness.contracts import MeasuredGripperFeedback, MeasuredGripperSample
from f1_harness.gripper_contact import GraspClosureEpisode
from f1_harness.live_scene import LiveSceneObserver


class GripperPlant:
    def __init__(self, verifier, store, scene_config):
        self.samples = deque(maxlen=64)
        self.sequence = 0
        cfg = dict(scene_config['geometry'], measured_gripper_contact=True,
                   measured_gripper_max_age_ns=int(scene_config['feedback_age_ms']*1e6))
        self.gateway = LiveSceneObserver.__new__(LiveSceneObserver)
        self.gateway.config = dict(scene_config, geometry=cfg)
        self.gateway.pipeline = SimpleNamespace(grasp_closure=GraspClosureEpisode())
        self.gateway.archive_bytes = 0
        self.gateway.store = store
        self.gateway.verifier = SimpleNamespace(call=lambda method, **kwargs: verifier.dispatch(method, kwargs))

    def observe(self, scene, context, *, measured_closure, blocked, verifier_available):
        # The actuator plant stops at the last field run's blocked encoder code.
        # Empty/free endpoints follow the actuator. All samples are simulation.
        stamp = scene.received_ns
        raw = 35 if blocked else int(round((100-measured_closure)*255/100))
        source = 'simulated-gripper-can'
        if not self.samples or stamp > self.samples[-1].received_ns:
            self.sequence += 1
            row = MeasuredGripperSample(source_instance=source, sequence=self.sequence, received_ns=stamp,
                kernel_received_realtime_ns=stamp, queue_delay_ns=0, time_valid=True, frame_format='can_fd',
                can_fd_flags=0, raw_position=raw, closure_percent=(255-raw)*100/255, velocity_raw=0,
                current_raw=2, data_hex=bytes([0,0,raw,0,2,0,0,0]).hex())
            self.samples.append(row)
        packet = MeasuredGripperFeedback.model_validate(dict(schema='f1-gripper-feedback-v1',
            source='socketcan_external',protocol='omnipicker_status_v1',side='right',interface='offline',
            response_id=769,source_instance=source,boot_id=scene.host_boot_id,clock_domain='host_monotonic',
            position_unit='closure_0_open_100_closed',position_resolution=100/255,running=True,
            received_count=self.sequence,rejected_count=0,latest=self.samples[-1],samples=tuple(self.samples)))
        raw_scene = scene.model_copy(update={'measured_gripper':packet,'measured_gripper_ref':None})
        cfg = self.gateway.config['geometry']
        proof = self.gateway.pipeline.grasp_closure.measure(raw_scene, context, cfg, at_ns=stamp)
        if verifier_available:
            self.gateway._offer_grasp_measurement(raw_scene,context,now_ns=stamp)
        target = cfg['object_id']
        objects = tuple(obj.model_copy(update={'held':proof['held'], 'eef_object_pose':
            obj.eef_object_pose if proof['held'] else None}) if obj.object_id==target else obj for obj in scene.objects)
        # Private grip evidence is archived by the gateway. The simulated
        # planning fixture receives the measured state and its scene geometry.
        return scene.model_copy(update={'objects':objects})
