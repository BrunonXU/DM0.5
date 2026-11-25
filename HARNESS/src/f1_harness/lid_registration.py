"""Coarse bucket registration from a measured slot and visible parts of a second rim."""
from dataclasses import dataclass

import cv2
import numpy as np

from .aperture_image import aperture_features
from .geometry import crop_intrinsics, transform
from .perception import measurement_region


def _region(shape, corners):
    mask = np.zeros(shape, np.uint8)
    cv2.fillConvexPoly(mask, np.int32(corners), 1)
    return measurement_region(mask.astype(bool), dict(kind='outer_ring', inner_px=3, outer_px=8))


def _surface(depth, rim, config, tolerance):
    k = crop_intrinsics(config['k_rgb'], config['roi'])
    y, x = np.nonzero(rim & np.isfinite(depth) & (depth > 0))
    if len(x) < 60:
        raise ValueError('slot_lid_depth_missing')
    points = np.linalg.solve(k, np.stack([x, y, np.ones(len(x))])).T * depth[y, x, None]
    # Isolate the lid from deeper samples through the opening.
    points = points[np.abs(points[:, 2] - np.median(points[:, 2])) < 2*tolerance]
    for _ in range(3):
        if len(points) < 35:
            raise ValueError('slot_lid_plane_missing')
        center = points.mean(axis=0)
        normal = np.linalg.svd(points-center, full_matrices=False)[2][-1]
        error = np.abs((points-center) @ normal)
        if np.quantile(error, .9) <= .003:
            break
        points = points[error <= np.quantile(error, .8)]
    if np.quantile(error, .9) > .003:
        raise ValueError('slot_lid_plane_unresolved')
    return k, center, normal


def _xyz(pixels, surface):
    k, center, normal = surface
    rays = np.linalg.solve(k, np.column_stack([pixels, np.ones(len(pixels))]).T).T
    denominator = rays @ normal
    if np.any(abs(denominator) < .1):
        raise ValueError('slot_lid_grazing_view')
    xyz = rays * ((center @ normal)/denominator)[:, None]
    if np.min(xyz[:, 2]) <= 0:
        raise ValueError('slot_lid_behind_camera')
    return xyz


def _rigid(old, new):
    a, b = old.mean(axis=0), new.mean(axis=0)
    u, _, vt = np.linalg.svd((old-a).T @ (new-b))
    if np.linalg.det(vt.T @ u.T) < 0:
        vt[-1] *= -1
    rotation = vt.T @ u.T
    return rotation, b-rotation@a


@dataclass
class LidRegistration:
    slot: np.ndarray
    rim_samples: np.ndarray
    contrast: np.ndarray
    camera_fixture: np.ndarray
    evidence: tuple

    @classmethod
    def from_landmarks(cls, rgb, depth, camera, pose, anchors, evidence, tolerance):
        """Seed only from a full independent three-landmark bucket registration."""
        features = aperture_features(rgb)
        slot_center = np.array(anchors['bucket-left-slot']['roi_center_px'])
        centers = np.float32([a['roi_center_px'] for a in anchors.values()])
        patch = cv2.convexHull(centers)
        slots = [s for s in features['square'] if np.linalg.norm(s['center_px']-slot_center) <= 5.]
        circles = [c for c in features['circle'] if cv2.pointPolygonTest(patch,tuple(c['center_px']),False) >= 0]
        if len(slots) != 1 or len(circles) != 1:
            raise ValueError('slot_and_circle_not_uniquely_registered')
        corners = np.array(slots[0]['corners_px'])
        center, axes, angle = circles[0]['ellipse']
        theta = np.arange(36)*2*np.pi/36
        a = np.deg2rad(angle)
        offsets = np.column_stack((np.cos(theta),np.sin(theta)))*(np.array(axes)/2.)
        offsets = offsets @ np.array([[np.cos(a), np.sin(a)], [-np.sin(a),np.cos(a)]])
        # Sample both sides of the circular lip and the physical support outside.
        samples = np.stack([center + offsets*scale for scale in (.78,1.22,1.38)])
        outer = _region(depth.shape, corners) | _region(depth.shape, center + offsets)
        surface = _surface(depth, outer, camera, tolerance)
        xyz = _xyz(samples.reshape(-1,2),surface).reshape(3,-1,3)
        gray = cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        inner, outer = np.rint(samples[:2]).astype(int)
        contrast = gray[outer[:,1],outer[:,0]].astype(float)-gray[inner[:,1],inner[:,0]]
        return cls(_xyz(corners,surface), xyz, contrast,
                   np.linalg.inv(transform(camera['base_camera']))@pose, evidence)

    def measure(self, rgb, depth, camera, tolerance, clearance):
        gray = cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        matches = []
        for slot in aperture_features(rgb)['square']:
            corners = np.asarray(slot['corners_px'])
            try:
                surface = _surface(depth, _region(depth.shape,corners),camera,tolerance)
                xyz = _xyz(corners,surface)
            except ValueError:
                continue
            k = surface[0]
            # Contour order is cyclic. Verify chirality by the circle and its lid depth.
            for reversed_winding in (False, True):
                oriented = xyz[::-1] if reversed_winding else xyz
                for offset in range(4):
                    ordered = np.roll(oriented,offset,axis=0)
                    rot, trans = _rigid(self.slot,ordered)
                    if np.max(np.linalg.norm(self.slot@rot.T+trans-ordered,axis=1)) > clearance:
                        continue
                    current = self.rim_samples@rot.T+trans
                    if np.min(current[:,:,2]) <= 0:
                        continue
                    projected = current@k.T
                    pixels = np.rint(projected[:,:,:2]/projected[:,:,2,None]).astype(int)
                    x,y = pixels[:,:,0],pixels[:,:,1]
                    if x.min()<0 or y.min()<0 or x.max()>=depth.shape[1] or y.max()>=depth.shape[0]:
                        continue
                    contrast = gray[y[1],x[1]].astype(float)-gray[y[0],x[0]]
                    supported = (self.contrast >= 15) & (contrast >= np.maximum(12,.45*self.contrast))
                    # Check several ring-depth pixels on the *current* second landmark.
                    distances = []
                    for dx,dy in ((0,0),(-1,0),(1,0),(0,-1),(0,1)):
                        z = depth[np.clip(y[2]+dy,0,depth.shape[0]-1),np.clip(x[2]+dx,0,depth.shape[1]-1)]
                        distances.append(abs(z-current[2,:,2]))
                    supported &= np.min(distances,axis=0) <= min(tolerance,.008)
                    indices = np.flatnonzero(supported)
                    if len(indices)<8:
                        continue
                    visible = current[2,indices]
                    if np.linalg.norm(np.ptp(visible,axis=0)) < .6*np.linalg.norm(np.ptp(current[2],axis=0)):
                        continue
                    delta = np.eye(4)
                    delta[:3,:3],delta[:3,3] = rot,trans
                    # Same face of the lid must be visible in both measurements.
                    base = transform(camera['base_camera'])@delta@self.camera_fixture
                    local = delta@self.camera_fixture
                    if local[:3,2]@local[:3,3] >= 0:
                        continue
                    matches.append((base,dict(source='current_slot_partial_circle_rgbd',rim_samples=len(indices),
                                       reference_evidence=self.evidence,aperture_measured=False)))
        if len(matches)!=1:
            raise ValueError('partial_lid_ambiguous' if matches else 'partial_lid_landmarks_unavailable')
        return matches[0]


def verify_remaining_anchors(pose, depth, masks, camera, reference_camera, reference_pose, anchors, tolerance):
    """A retained label with current depth can veto a conflicting new body pose."""
    inverse = np.linalg.inv(crop_intrinsics(camera['k_rgb'],camera['roi']))
    old_to_new = pose@np.linalg.inv(transform(reference_pose))@transform(reference_camera)
    now = transform(camera['base_camera'])
    for name, reference in anchors.items():
        mask = masks.get(name)
        if mask is None or not np.any(mask):
            continue
        y,x = np.nonzero(mask)
        values = depth[mask]
        valid = values[np.isfinite(values) & (values>0)]
        if len(x)<20 or len(valid)<20:
            continue
        old = (inverse@np.r_[reference['roi_center_px'],1.])*reference['depth_m']
        new = (inverse@np.array([x.mean(),y.mean(),1.]))*np.median(valid)
        if np.linalg.norm((old_to_new@np.r_[old,1.])[:3] - (now@np.r_[new,1.])[:3]) > tolerance:
            raise ValueError('partial_lid_prompted_anchor_disagrees:'+name)
