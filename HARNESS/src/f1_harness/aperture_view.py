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
        self.fallback_issued = False
        self.fallback_issued_tcp = None
        self.fallback_used = False

    def _reset_fallback(self):
        self.fallback_issued = False
        self.fallback_issued_tcp = None
        self.fallback_used = False

    def _projected_width(self, direction, target, eef, tcp):
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
        return max(r for _, r in intervals) - min(l for l, _ in intervals)

    def update(self, scene, context, world_eef, head_pose=None):
        self.move = None
        identity = tuple(context.get(key) for key in
                         ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'task_generation', 'epoch'))
        scope = (*identity, scene.acquisition_instance, scene.host_boot_id, scene.calibration_id)
        if (context.get('authorized') is not True or context.get('placement_verified')
                or None in scope or scene.synthetic):
            self.scope = self.anchor = None
            self._reset_fallback()
            return
        # A regrasp within one owner epoch starts a different held-object episode.
        scope += ((context.get('grasp_contact') or {}).get('request_id'),)
        if scope != self.scope:
            self.scope, self.anchor = scope, None
            self._reset_fallback()
        current = {o.object_id: o for o in scene.objects}
        target, hole = current.get(self.object_id), current.get(self.hole_id)
        if target is not None and target.held is False:
            self.anchor = None
            self._reset_fallback()
        if self.fallback_issued and context.get('action_published'):
            self.fallback_used = True
        if (target is None or target.held is not True or target.eef_object_pose is None
                or not target.visible or not target.pose):
            return
        hashes = {f.blob_sha256 for f in scene.frames}
        if not target.evidence_ids or not set(target.evidence_ids).issubset(hashes):
            return
        eef = self.native_world @ transform(world_eef)
        tcp = eef @ self.envelope['tool']
        if (self.fallback_issued and not self.fallback_used
                and self.fallback_issued_tcp is not None and not context.get('action_published')
                and np.linalg.norm(tcp[:3, 3] - self.fallback_issued_tcp) > self.envelope['resolution']):
            self.fallback_used = True
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
            width = self._projected_width(direction, target, eef, tcp)
            step = min(distance, (width + self.envelope['hole_width']) / 2)
            goal = tcp.copy()
            goal[:3, 3] += direction * step
            self.move = dict(target_frame='world', target_tcp_pose=goal.ravel().tolist(),
                             source_observation_id=self.anchor['source_observation_id'],
                             source_frame_hashes=self.anchor['source_frame_hashes'],
                             current_observation_id=scene.observation_id,
                             purpose='noncontact_lateral_aperture_reobservation')
        elif (self.anchor is None and not self.fallback_used and not context.get('action_published')
                and head_pose is not None and (
                    any(f.camera == 'head' for f in scene.frames)
                    or (getattr(scene, 'fixture', None) is not None
                        and getattr(scene.fixture, 'source', None) == 'retained_static_fixture'))):
            normal = np.asarray(self.envelope['normal'], float)
            n_norm = np.linalg.norm(normal)
            if n_norm <= np.finfo(float).eps:
                return
            n = normal / n_norm
            ray = transform(target.pose)[:3, 3] - transform(head_pose)[:3, 3]
            ray_plane = ray - n * np.dot(ray, n)
            if np.linalg.norm(ray_plane) <= np.finfo(float).eps:
                return
            perp = np.cross(n, ray_plane)
            p_norm = np.linalg.norm(perp)
            if p_norm <= np.finfo(float).eps:
                return
            direction = perp / p_norm
            if abs(direction[1]) <= np.finfo(float).eps:
                return
            if direction[1] > 0:
                direction = -direction
            width = self._projected_width(direction, target, eef, tcp)
            step = (width + self.envelope['hole_width']) / 2
            goal = tcp.copy()
            goal[:3, 3] += direction * step
            self.move = dict(target_frame='world', target_tcp_pose=goal.ravel().tolist(),
                             source_observation_id=scene.observation_id,
                             source_frame_hashes=list(target.evidence_ids),
                             current_observation_id=scene.observation_id,
                             purpose='noncontact_lateral_aperture_reobservation_without_prior_view')
            self.fallback_issued = True
            self.fallback_issued_tcp = tcp[:3, 3].copy()
