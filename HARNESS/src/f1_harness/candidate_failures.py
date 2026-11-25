"""Reuse only deterministic failed calculations with exactly identical inputs.

No pose rounding, age gate, success cache, execution result or authorization is
stored. A different joint measurement, geometry, parameter or scope recomputes.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from threading import Lock


DETERMINISTIC_FAILURES = frozenset((
    'bounded_IK_did_not_reach_goal', 'actual_IK_candidate_collision_rejected',
    'actual_IK_path_tool_sweep_blocked', 'actual_IK_path_scene_sweep_blocked',
    'actual_IK_path_attached_object_sweep_blocked', 'scene_sweep_blocked',
    'no_clear_square_symmetry_route', 'grasp_height_outside_registered_object',
    'grasp_has_no_placement_route', 'grasp_lift_sweep_blocked',
    'pregrasp_target_outside_wrist_view', 'return_route_sweep_blocked',
))


def candidate_key(task, fixture, intent, observation, scene, held, full, variant, *, computation_only=False):
    """Bind physical planning inputs; changing frame/request IDs alone is not new geometry."""
    value=dict(task=task.model_dump(), fixture=fixture, skill=intent.skill,
        scope={k:getattr(intent,k) for k in ('session_id','owner_instance_id','epoch','calibration_id')},
        acquisition=observation.acquisition_instance, boot=observation.host_boot_id,
        geometry_scope=observation.geometry_scope, geometry_camera=observation.geometry_camera,
        scene=asdict(scene), held=held, full=full, variant=variant,
        parameters=intent.parameters.model_dump())
    if computation_only:
        # Measurement labels do not change an IK calculation. Keep every
        # geometric number, semantic state and source identity; admission and
        # fresh scene revalidation still consume the untouched observations.
        labels={'observation_id','source_observation_id','evidence_ids','source_evidence_ids',
            'measured_at_ns','observation_received_ns','state_received_ns','received_ns',
            'source_ns','sequence','blob_sha256','source_rgb_sha256','source_depth_sha256'}
        def geometry_only(item):
            if isinstance(item,dict):return {k:geometry_only(v) for k,v in item.items() if k not in labels}
            if isinstance(item,(tuple,list)):return [geometry_only(v) for v in item]
            return item
        value['scene']=geometry_only(value['scene'])
        value['camera_identity']=[{key:getattr(frame,key) for key in (
            'camera','stream','frame_id','width','height','encoding','depth_scale_m',
            'camera_info_sha256','clock_domain','source_epoch','publisher_id')} for frame in observation.frames]
    def arrays(item):
        import numpy as np
        if isinstance(item,np.ndarray):return item.tolist()
        if isinstance(item,np.generic):return item.item()
        raise TypeError(type(item).__name__)
    try:raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False,default=arrays)
    except (TypeError,ValueError):return None  # Unsupported cache inputs still take the normal planner.
    return hashlib.sha256(raw.encode()).hexdigest()


class CandidateFailures:
    def __init__(self, capacity=128):
        self.capacity=capacity
        self.entries=OrderedDict()
        self.lock=Lock()

    def get(self,key):
        with self.lock:
            if key is None or key not in self.entries:return None
            value=deepcopy(self.entries[key]);self.entries.move_to_end(key)
        value['diagnostic']['metrics'].update(reused_failure=True,
            previous_request_id=value['request_id'],
            recovery_hint='change_candidate_or_observe_changed_geometry')
        return value['diagnostic']

    def remember(self,key,detail,request_id):
        code=detail['code']
        if code=='grasp_continuation_unreachable':
            code=detail.get('metrics',{}).get('continuation_code')
        if key is None or code not in DETERMINISTIC_FAILURES:return
        with self.lock:
            self.entries[key]=dict(diagnostic=deepcopy(detail),request_id=request_id)
            self.entries.move_to_end(key)
            while len(self.entries)>self.capacity:self.entries.popitem(last=False)
