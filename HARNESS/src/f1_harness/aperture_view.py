"""Remember where the held arm was when the target aperture was visible.

This is a source of world-frame search goals only. The native planner checks
the current scene and payload for each move, and perception measures the hole
again after the move.
"""
import numpy as np

from .geometry import transform


def planner_view_geometry(plan, scene):
    """Read the same TCP, robot envelope and table used by the site's planner."""
    fixture = plan.get('fixture', {})
    task = plan.get('task', {})
    geometry = scene.get('geometry', {})
    if (not {'move_tcp', 'reobserve'}.issubset(task.get('allowed_skills', ()))
            or not fixture.get('cartesian_motion')
            or fixture.get('frame_chain') != scene.get('frame_chain')
            or any(task.get(k) != geometry.get(k) for k in ('object_id', 'hole_id', 'calibration_id'))
            or fixture.get('eef_tip_reference') is None):
        return None
    table = next((box for box in fixture.get('obstacles', ()) if
                  box.get('role') == 'table' and box.get('type') == 'halfspace'), None)
    if not table or not (fixture.get('object_local_box') and fixture.get('hole_inner_dimensions_m')):
        return None
    tolerances = fixture.get('revalidation', {})
    resolution = tolerances.get('free_space_goal_tolerance_m', tolerances.get('xy_tolerance_m'))
    if not resolution or resolution <= 0:
        return None
    return dict(tool=transform(fixture['eef_tip_reference']), normal=np.asarray(table['normal']),
                tool_boxes=fixture.get('tool_local_boxes') or (), object_box=fixture['object_local_box'],
                hole_width=max(fixture['hole_inner_dimensions_m']),
                resolution=resolution)


class ApertureViewpoint:
    def __init__(self, envelope, frames, geometry):
        self.envelope = envelope
        self.native_world = np.linalg.inv(frames.world_native)
        self.object_id, self.hole_id = geometry['object_id'], geometry['hole_id']
        self.scope = None
        self.anchor = None
        self.move = None

    def update(self, scene, context, world_eef, head_pose=None):
        self.move = None
        identity = tuple(context.get(key) for key in
                         ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'task_generation', 'epoch'))
        scope = (*identity, scene.acquisition_instance, scene.host_boot_id, scene.calibration_id)
        if (context.get('authorized') is not True or context.get('placement_verified')
                or None in scope or scene.synthetic):
            self.scope = self.anchor = None
            return
        # A regrasp within one owner epoch starts a different held-object episode.
        scope += ((context.get('grasp_contact') or {}).get('request_id'),)
        if scope != self.scope:
            self.scope, self.anchor = scope, None
        current = {o.object_id: o for o in scene.objects}
        target, hole = current.get(self.object_id), current.get(self.hole_id)
        if target is not None and target.held is False:
            self.anchor = None
        if (target is None or target.held is not True or target.eef_object_pose is None
                or not target.visible or not target.pose):
            return
        hashes = {f.blob_sha256 for f in scene.frames}
        if not target.evidence_ids or not set(target.evidence_ids).issubset(hashes):
            return
        eef = self.native_world @ transform(world_eef)
        tcp = eef @ self.envelope['tool']
        if hole is not None and hole.pose is not None:
            if (hole.visible and hole.evidence_ids and set(hole.evidence_ids).issubset(hashes)
                    and not context.get('action_published')):
                self.anchor = dict(tcp=tcp, source_observation_id=scene.observation_id,
                                   source_frame_hashes=list(hole.evidence_ids))
            return
        if self.anchor and not context.get('action_published'):
            # Only borrow the direction from the last clear view. Moving to
            # that entire pose could lower the payload all the way to the table.
            normal = self.envelope['normal']
            delta = self.anchor['tcp'][:3, 3] - tcp[:3, 3]
            # A vertical previous displacement can uncover the aperture in an
            # oblique camera as well. Transfer that observed change of sight
            # line into the table plane when this head pose was measured.
            axis = normal
            if head_pose is not None and any(f.camera == 'head' for f in scene.frames):
                ray = transform(target.pose)[:3, 3] - transform(head_pose)[:3, 3]
                if abs(ray @ normal) > np.finfo(float).eps:
                    axis = ray / (ray @ normal)
            delta -= axis * np.dot(delta, normal)
            distance = np.linalg.norm(delta)
            if distance <= self.envelope['resolution']:
                return  # The previous view is already within the owner's arrival tolerance.
            direction = delta / distance
            # Width of the measured payload and tool silhouette along the
            # proposed displacement. Half the combined aperture/occluder width
            # clears their overlapping centers under an orthographic view.
            intervals = []
            for pose, box in [(transform(target.pose), self.envelope['object_box']),
                              *((eef, box) for box in self.envelope['tool_boxes'])]:
                if 'pose' in box:
                    pose = pose @ transform(box['pose'])
                axes = direction @ pose[:3, :3]
                low = axes * np.asarray(box['min'])
                high = axes * np.asarray(box['max'])
                center = direction @ (pose[:3, 3] - tcp[:3, 3])
                intervals.append((center + np.minimum(low, high).sum(),
                                  center + np.maximum(low, high).sum()))
            width = max(r for _, r in intervals) - min(l for l, _ in intervals)
            step = min(distance, (width + self.envelope['hole_width']) / 2)
            goal = tcp.copy()
            goal[:3, 3] += direction * step
            self.move = dict(target_frame='world', target_tcp_pose=goal.ravel().tolist(),
                             source_observation_id=self.anchor['source_observation_id'],
                             source_frame_hashes=self.anchor['source_frame_hashes'],
                             current_observation_id=scene.observation_id,
                             purpose='noncontact_lateral_aperture_reobservation')
