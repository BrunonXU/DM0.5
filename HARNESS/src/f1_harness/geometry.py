"""Metric geometry, ROI intrinsics, RGB-D registration and conservative scene screening."""
from __future__ import annotations

import numpy as np
from functools import lru_cache

HEAD_ROI = (307, 28, 1229, 720)


@lru_cache(maxsize=256)
def _validate_transform_bytes(payload):
    """Cache only validation of exact values, never a mutable pose or verdict.

    Tool/obstacle frames repeat across path samples. A new measurement or an
    in-place edit gets a different byte key; no quantization or tolerance-based
    reuse is allowed. Exceptions are not cached, and memory is bounded.
    """
    matrix = np.frombuffer(payload, dtype=float).reshape(4, 4)
    if not np.isfinite(matrix).all() or not np.allclose(matrix[3], (0, 0, 0, 1), atol=1e-8):
        raise ValueError("invalid homogeneous transform")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5):
        raise ValueError("rotation must be orthonormal and right handed")


def transform(value):
    matrix = np.asarray(value, dtype=float).reshape(4, 4)
    _validate_transform_bytes(matrix.tobytes())
    return matrix


def crop_intrinsics(k, roi=HEAD_ROI, *, scale=(1., 1.), padding=(0., 0.)):
    k = np.asarray(k, dtype=float).reshape(3, 3).copy()
    x0, y0, x1, y1 = roi
    if x1 <= x0 or y1 <= y0 or min(scale) <= 0:
        raise ValueError("invalid image transform")
    affine = np.array([[scale[0], 0, padding[0] - scale[0] * x0],
                       [0, scale[1], padding[1] - scale[1] * y0], [0, 0, 1.]])
    return affine @ k


def eef_goal(object_goal, eef_object):
    return transform(object_goal) @ np.linalg.inv(transform(eef_object))


@lru_cache(maxsize=8)
def _depth_rays(shape, intrinsics, distortion):
    """Bounded immutable camera-dependent work; no cross-camera key omission."""
    import cv2
    y,x=np.indices(shape)
    pixels=np.stack((x,y),axis=-1).astype(np.float64).reshape(-1,1,2)
    rays=cv2.undistortPoints(pixels,np.asarray(intrinsics).reshape(3,3),
        np.asarray(distortion)).reshape(*shape,2)
    rays.setflags(write=False)
    return rays


def _project_color(points,k,distortion):
    """OpenCV pinhole/rational projection without its unused large Jacobian."""
    d=np.asarray(distortion,float).reshape(-1)
    if not np.any(d):
        return np.column_stack((k[0,0]*points[:,0]/points[:,2]+k[0,2],
                                k[1,1]*points[:,1]/points[:,2]+k[1,2]))
    if len(d) not in (0,4,5,8):
        import cv2
        return cv2.projectPoints(points,np.zeros(3),np.zeros(3),k,d)[0].reshape(-1,2)
    c=np.zeros(8);c[:len(d)]=d
    x,y=points[:,0]/points[:,2],points[:,1]/points[:,2]
    r2=x*x+y*y;r4=r2*r2;r6=r4*r2
    radial=(1+c[0]*r2+c[1]*r4+c[4]*r6)/(1+c[5]*r2+c[6]*r4+c[7]*r6)
    u=x*radial+2*c[2]*x*y+c[3]*(r2+2*x*x)
    v=y*radial+c[2]*(r2+2*y*y)+2*c[3]*x*y
    return np.column_stack((k[0,0]*u+k[0,2],k[1,1]*v+k[1,2]))


@lru_cache(maxsize=4)
def _rgb_depth_rays(shape, intrinsics, distortion, rotation):
    """Rotate calibrated rays once; depth and frame validity are never cached."""
    xy=_depth_rays(shape,intrinsics,distortion)
    rays=np.concatenate((xy,np.ones((*shape,1))),axis=2)
    rays=rays@np.asarray(rotation).reshape(3,3).T
    rays.setflags(write=False)
    return rays


def register_depth(depth, k_depth, d_depth, t_rgb_depth, k_rgb, d_rgb, rgb_size, scale_m):
    """Project native depth into RGB with z-buffer; never resize depth as calibration."""
    depth = np.asarray(depth)
    if depth.ndim != 2 or not 0 < scale_m <= 1:
        raise ValueError("invalid depth or scale")
    ys, xs = np.nonzero(np.isfinite(depth) & (depth > 0))
    if not len(xs):
        return np.zeros((rgb_size[1], rgb_size[0]), np.float32)
    k_depth, k_rgb = np.asarray(k_depth, float).reshape(3, 3), np.asarray(k_rgb, float).reshape(3, 3)
    t = transform(t_rgb_depth)
    rays=_rgb_depth_rays(tuple(depth.shape),tuple(k_depth.ravel()),
                         tuple(np.asarray(d_depth,float).ravel()),tuple(t[:3,:3].ravel()))[ys,xs]
    z = depth[ys, xs].astype(float) * scale_m
    points = rays * z[:,None] + t[:3,3]
    points = points[points[:, 2] > 0]
    width, height = rgb_size
    output = np.full((height, width), np.inf, dtype=np.float64)
    if len(points):
        uv=np.rint(_project_color(points,k_rgb,d_rgb)).astype(int)
        valid = (uv[:, 0] >= 0) & (uv[:, 0] < width) & (uv[:, 1] >= 0) & (uv[:, 1] < height)
        uv, z = uv[valid], points[valid, 2]
        np.minimum.at(output, (uv[:, 1], uv[:, 0]), z)
    output[~np.isfinite(output)] = 0
    return output.astype(np.float32)


def masked_points(depth_m, mask, k):
    depth_m, mask = np.asarray(depth_m), np.asarray(mask, bool)
    if depth_m.shape != mask.shape:
        raise ValueError("mask/depth must share calibrated pixels")
    y, x = np.nonzero(mask & np.isfinite(depth_m) & (depth_m > 0))
    if len(x) < 20:
        raise ValueError("insufficient valid depth")
    k = np.asarray(k, float).reshape(3, 3)
    rays = np.column_stack((x, y, np.ones(len(x)))) @ np.linalg.inv(k).T
    return rays * depth_m[y, x, None]


def align_square_pose(pose, reference):
    """Choose the same physical square's nearest local-Z coordinate branch.

    Returns the aligned transform and the exact coordinate change. Translation,
    tilt and measured timestamps are never filtered or replaced.
    """
    pose,reference=transform(pose),transform(reference)
    candidates=[]
    for angle in (0.,np.pi/2,np.pi,3*np.pi/2):
        c,s=np.cos(angle),np.sin(angle)
        symmetry=np.eye(4);symmetry[:2,:2]=[[c,-s],[s,c]]
        aligned=pose@symmetry
        score=float(np.trace(reference[:3,:3].T@aligned[:3,:3]))
        candidates.append((score,aligned,symmetry))
    _,aligned,symmetry=max(candidates,key=lambda item:item[0])
    return aligned,symmetry


def fit_block_pose(points, *, max_plane_residual_m=.003, normal_prior=None):
    """Fit an observed planar face; yaw remains symmetric for a square."""
    from .face_boundary import edge_direction
    points = np.asarray(points, float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 20 or not np.isfinite(points).all():
        raise ValueError("invalid point cloud")
    center = np.median(points, axis=0)
    distances = np.linalg.norm(points - center, axis=1)
    trimmed = points[distances <= np.quantile(distances, .95)]
    center = trimmed.mean(axis=0)
    _, _, axes = np.linalg.svd(trimmed - center, full_matrices=False)
    normal = axes[2]
    if normal[2] > 0:
        normal = -normal
    x_axis = axes[0]
    if normal_prior is not None:
        normal=np.asarray(normal_prior,float)
        if normal.shape!=(3,) or not np.isfinite(normal).all() or np.linalg.norm(normal)<1e-8:
            raise ValueError('invalid_plane_normal_prior')
        normal=normal/np.linalg.norm(normal)
        x_axis=x_axis-normal*np.dot(x_axis,normal)
        if np.linalg.norm(x_axis)<1e-8:raise ValueError('degenerate_plane_basis')
        x_axis/=np.linalg.norm(x_axis)
    y_axis = np.cross(normal, x_axis)
    # The plane fit and its residual retain the old robust support. For yaw,
    # radial trimming would round off square corners and amplify depth noise.
    x_axis,boundary_fit=edge_direction(points,center,x_axis,y_axis)
    y_axis=np.cross(normal,x_axis)
    t = np.eye(4)
    t[:3, :3], t[:3, 3] = np.column_stack((x_axis, y_axis, normal)), center
    residual = float(np.quantile(np.abs((trimmed - center) @ normal), .95))
    if residual > max_plane_residual_m:
        raise ValueError("surface is not a reliable planar block face")
    return dict(pose=t.tolist(), residual_p95_m=residual, yaw_equivalence_degrees=[0, 90, 180, 270],
                observed_surface_only=True,boundary_fit=boundary_fit)


def boxes_overlap(a_min, a_max, b_min, b_max):
    return bool(np.all(np.asarray(a_min) <= np.asarray(b_max)) and
                np.all(np.asarray(b_min) <= np.asarray(a_max)))


def _points_clear(points, padding, obstacles, *, separating_axes=None):
    """Conservative corner bound in each obstacle's own frame / table halfspace."""
    # Bucket pieces often share one frame. Reuse only this call's exact point
    # projection; neither obstacle bounds nor clearance decisions are cached.
    # A later call always uses its new points, padding and measured frame.
    projected={}
    for obstacle in obstacles:
        if obstacle.get('type')=='halfspace':
            normal=np.asarray(obstacle['normal'],float);point=np.asarray(obstacle['point'],float)
            if (normal.shape!=(3,) or point.shape!=(3,) or not np.isfinite([normal,point]).all()
                    or not np.isclose(np.linalg.norm(normal),1.,atol=1e-5)):
                raise ValueError('invalid_obstacle_halfspace')
            distance=float(np.min((points-point)@normal))
            if distance < padding or (padding>0 and distance==padding):return False
        else:
            pose=transform(obstacle.get('pose',np.eye(4)))
            key=pose.tobytes()
            cached=projected.get(key)
            if cached is None:
                local=(points-pose[:3,3])@pose[:3,:3]
                cached=(local,local.min(0)-padding,local.max(0)+padding)
                if len(projected)<32:projected[key]=cached
            local,lower,upper=cached
            if not boxes_overlap(lower,upper,obstacle['min'],obstacle['max']):continue
            if separating_axes is not None:
                # AABB overlap is only a broad-phase result for a rotated tool.
                # Any separating axis proves the entire corner convex hull clear.
                # Preserve the original obstacle-frame padding cube, including
                # the rotation curvature bound; never just check end poses.
                axes=np.asarray(separating_axes)@pose[:3,:3]
                axes=np.concatenate([axes,np.cross(axes[:,None,:],np.eye(3)[None,:,:]).reshape(-1,3)])
                axes=axes[np.linalg.norm(axes,axis=1)>1e-10]
                low,high=np.asarray(obstacle['min']),np.asarray(obstacle['max'])
                center=(low+high)/2;extent=(high-low)/2
                projections=local@axes.T
                expansion=padding*np.abs(axes).sum(axis=1)
                midpoint=axes@center;radius=np.abs(axes)@extent
                if np.any((projections.max(axis=0)+expansion<midpoint-radius) |
                          (projections.min(axis=0)-expansion>midpoint+radius)):continue
            return False
    return True


def tool_sweep_clear(start, end, eef_tip_reference, radius_m, obstacles, *, margin_m, local_boxes=None):
    """Sweep registered local tool boxes along translation and shortest rotation.

    Each short rotation interval includes a curvature bound; checking only the
    two end orientations can miss the corner swept through an obstacle.
    """
    a,b,tip=map(transform,(start,end,eef_tip_reference))
    if not np.isfinite([radius_m,margin_m]).all() or radius_m<=0 or margin_m<0:
        raise ValueError('positive_tool_envelope_required')
    if local_boxes is not None:
        from itertools import product
        from scipy.spatial.transform import Rotation
        if not isinstance(local_boxes,(list,tuple)) or not local_boxes:
            raise ValueError('explicit_tool_boxes_required')
        rotation=Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).as_rotvec()
        angle=float(np.linalg.norm(rotation));count=max(1,int(np.ceil(angle/np.deg2rad(5))))
        fractions=np.linspace(0,1,count+1)
        rotations=a[:3,:3]@Rotation.from_rotvec(fractions[:,None]*rotation).as_matrix()
        positions=a[:3,3]+fractions[:,None]*(b[:3,3]-a[:3,3])
        for box in local_boxes:
            low,high=np.asarray(box['min'],float),np.asarray(box['max'],float)
            if low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all() or np.any(low>=high):
                raise ValueError('invalid_tool_box')
            corners=np.array(list(product(*zip(low,high))))
            offset=transform(box.get('pose',np.eye(4)))
            corners=corners@offset[:3,:3].T+offset[:3,3]
            # For a corner p and rotation interval dtheta, the second derivative
            # norm is <= |p|*dtheta^2, giving chord interpolation error <= /8.
            radius=float(np.max(np.linalg.norm(corners,axis=1)))
            def interval_clear(t0,r0,p0,t1,r1,p1,depth=0):
                # Coarse convex/curvature envelopes may overlap a nearby target
                # while the actual rotation arc remains clear. Refine only that
                # unresolved interval; every accepted child keeps the same
                # physical margin and its own conservative curvature bound.
                padding=margin_m+radius*(angle*(t1-t0))**2/8
                world=np.concatenate([corners@r.T+p for r,p in ((r0,p0),(r1,p1))])
                axes=np.concatenate([(r@offset[:3,:3]).T for r in (r0,r1)])
                if _points_clear(world,padding,obstacles,separating_axes=axes):return True
                if depth>=4:return False
                middle=(t0+t1)/2
                rm=a[:3,:3]@Rotation.from_rotvec(middle*rotation).as_matrix()
                pm=a[:3,3]+middle*(b[:3,3]-a[:3,3])
                return (interval_clear(t0,r0,p0,middle,rm,pm,depth+1)
                        and interval_clear(middle,rm,pm,t1,r1,p1,depth+1))
            for index in range(count):
                if not interval_clear(fractions[index],rotations[index],positions[index],
                        fractions[index+1],rotations[index+1],positions[index+1]):return False
        return True
    points=np.array([a[:3,3],b[:3,3],(a@tip)[:3,3],(b@tip)[:3,3]])
    return _points_clear(points,radius_m+margin_m,obstacles)


def tool_margin_departure_clear(start, end, eef_tip_reference, radius_m, obstacles, *,
                                margin_m, local_boxes, target_object_id,
                                departure_reference=None,departure_goal=None):
    """Prove one segment exits a target's padding without moving toward it.

    Only the identified target's positive, uninflated separation may replace
    the ordinary starting padding. Every tool box needs a fixed separating
    axis whose clearance cannot decrease anywhere along the shortest rotation
    arc. The endpoint must restore the full margin against every obstacle.
    This is not a general collision exemption or a multi-segment escape mode.
    """
    from itertools import product
    from scipy.spatial.transform import Rotation
    if not target_object_id or not local_boxes:return False
    a,b=map(transform,(start,end))
    if (departure_reference is None)!=(departure_goal is None):return False
    reference=a if departure_reference is None else transform(departure_reference)
    goal=b if departure_goal is None else transform(departure_goal)
    if not tool_sweep_clear(goal,goal,eef_tip_reference,radius_m,obstacles,
                           margin_m=margin_m,local_boxes=local_boxes):return False
    angle=float(np.linalg.norm(Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).as_rotvec()))
    for obstacle in obstacles:
        if tool_sweep_clear(a,b,eef_tip_reference,radius_m,(obstacle,),
                            margin_m=margin_m,local_boxes=local_boxes):continue
        if (obstacle.get('role')!='target_object'
                or obstacle.get('object_id')!=target_object_id):return False
        if obstacle.get('type')=='halfspace':
            normal=np.asarray(obstacle['normal'],float);point=np.asarray(obstacle['point'],float)
            if normal.shape!=(3,) or point.shape!=(3,) or not np.isfinite([normal,point]).all() or not np.isclose(np.linalg.norm(normal),1):return False
            for box in local_boxes:
                offset=transform(box.get('pose',np.eye(4)))
                corners=np.array(list(product(*zip(box['min'],box['max']))))@offset[:3,:3].T+offset[:3,3]
                initial=(corners@reference[:3,:3].T+reference[:3,3]-point)@normal
                current=(corners@a[:3,:3].T+a[:3,3]-point)@normal
                rotation_bound=angle*float(np.max(np.linalg.norm(corners,axis=1)))
                if (np.min(initial)<=0 or np.any(current<initial-1e-9)
                        or float((b[:3,3]-a[:3,3])@normal)+1e-12<rotation_bound):return False
            continue
        if departure_reference is not None:return False  # Prefix certificate is for a fixed measured face only.
        frame=transform(obstacle.get('pose',np.eye(4)))
        low,high=np.asarray(obstacle['min'],float),np.asarray(obstacle['max'],float)
        if low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all() or np.any(low>=high):return False
        other=np.array(list(product(*zip(low,high))))@frame[:3,:3].T+frame[:3,3]
        for box in local_boxes:
            offset=transform(box.get('pose',np.eye(4)))
            corners=np.array(list(product(*zip(box['min'],box['max']))))@offset[:3,:3].T+offset[:3,3]
            world=corners@a[:3,:3].T+a[:3,3]
            tool_axes=(a[:3,:3]@offset[:3,:3]).T;other_axes=frame[:3,:3].T
            axes=np.concatenate((tool_axes,other_axes,
                np.cross(tool_axes[:,None,:],other_axes[None,:,:]).reshape(-1,3)))
            axes=axes[np.linalg.norm(axes,axis=1)>1e-10]
            axes=axes/np.linalg.norm(axes,axis=1)[:,None]
            axes=np.concatenate((axes,-axes))
            gap=(world@axes.T).min(0)-(other@axes.T).max(0)
            # |d(R(t)p)/dt| <= angle*|p| for all t, so this bound
            # proves monotonic clearance for every corner, not just endpoints.
            rotation_bound=angle*float(np.max(np.linalg.norm(corners,axis=1)))
            departure=axes@(b[:3,3]-a[:3,3])
            if not np.any((gap>0)&(departure>=rotation_bound)):return False
    return True


def attached_object_sweep_clear(start,end,eef_object,object_local_box,obstacles,*,margin_m,
                                support_contact_tolerance_m=None,support_contact_direction='depart'):
    """Check an attached object, optionally departing or approaching its support.

    Approach uses the same continuous monotonic clearance proof in reverse.
    It only changes the object's registered support-plane contact allowance;
    the tool and every other obstacle retain their ordinary sweep checks.
    """
    if support_contact_direction not in ('depart','approach'):
        raise ValueError('invalid_support_contact_direction')
    if not isinstance(object_local_box,dict):raise ValueError('registered_object_box_required')
    box=dict(min=object_local_box['min'],max=object_local_box['max'],pose=transform(eef_object))
    if support_contact_tolerance_m is not None:
        from itertools import product
        tolerance=float(support_contact_tolerance_m)
        if not np.isfinite(tolerance) or tolerance<0:raise ValueError('invalid_support_contact_tolerance')
        first,last=(transform(end),transform(start)) if support_contact_direction=='approach' else (transform(start),transform(end))
        a,b=first@transform(eef_object),last@transform(eef_object)
        corners=np.array(list(product(*zip(box['min'],box['max']))))
        remaining=[]
        for obstacle in obstacles:
            if obstacle.get('type')!='halfspace' or obstacle.get('role')!='table':
                remaining.append(obstacle);continue
            normal=np.asarray(obstacle['normal'],float);point=np.asarray(obstacle['point'],float)
            distances=[float(np.min((corners@t[:3,:3].T+t[:3,3]-point)@normal)) for t in (a,b)]
            if min(distances)>margin_m:
                # Once detached, the ordinary inflated sweep is the stronger
                # condition. Small tracking rotations need not keep ascending.
                remaining.append(obstacle);continue
            # A coarse support-contact hypothesis may start within the registered
            # object margin, but may never descend further or borrow this rule later.
            baseline=0.
            reference=obstacle.get('support_departure_reference')
            if reference is not None and support_contact_direction=='depart':
                # A completed approach can preserve an already admissible
                # support relation across a bounded change of measured view.
                # Recompute the source proof here, including in native FK
                # sweeps. The fixed baseline never follows each segment.
                measured=transform(reference['measured_object_pose'])
                current=corners@measured[:3,:3].T+measured[:3,3]
                if reference.get('schema')=='f1-coarse-support-reference-v1':
                    prior=transform(reference['previous_object_pose'])
                    old=corners@prior[:3,:3].T+prior[:3,3]
                    old_gap=float(np.min((old-point)@normal))
                    if (not reference.get('source_observation_id')
                            or not -tolerance<=old_gap<=tolerance
                            or np.max(np.linalg.norm(current-old,axis=1))>reference['maximum_change_m']):return False
                elif reference.get('schema')=='f1-observed-model-departure-v1':
                    gaps=(current-point)@normal
                    if (not reference.get('source_observation_id')
                            or len(reference.get('source_evidence_ids',()))!=2
                            or reference.get('held') is True or reference.get('observed_open_gripper') is not True
                            or not float(gaps.min())<0<float(gaps.max())
                            or not np.isclose(reference.get('model_gap_m'),float(gaps.min()),rtol=0,atol=1e-9)):
                        return False
                else:return False
                baseline=min(0.,float(np.min((current-point)@normal)))
            if distances[0]<baseline-tolerance or distances[1]<distances[0]-1e-9:return False
            # A generic curvature-inflated box always penetrates a plane that
            # touches its starting corner. Prove this support departure instead:
            # translation's normal speed must dominate every rotating corner's
            # possible downward speed throughout the shortest rotation arc.
            from scipy.spatial.transform import Rotation
            angle=float(np.linalg.norm(Rotation.from_matrix(first[:3,:3].T@last[:3,:3]).as_rotvec()))
            offset=transform(eef_object)
            local_corners=corners@offset[:3,:3].T+offset[:3,3]
            downward_bound=angle*float(np.max(np.linalg.norm(local_corners,axis=1)))
            separation=float((last[:3,3]-first[:3,3])@normal)
            if (support_contact_direction=='approach' and separation<=1e-12
                    and np.linalg.norm(last[:3,3]-first[:3,3])>1e-12):return False
            if separation+1e-12<downward_bound:return False
        obstacles=remaining
    return tool_sweep_clear(start,end,np.eye(4),1.,obstacles,margin_m=margin_m,local_boxes=(box,))


def swept_box_clear(start, end, half_extent, obstacles, *, uncertainty_m):
    """Conservative swept AABB for a translating bound; no continuous robot proof."""
    start, end, extent = np.asarray(start, float), np.asarray(end, float), np.asarray(half_extent, float)
    if (start.shape != (3,) or end.shape != (3,) or extent.shape != (3,) or np.any(extent <= 0)
            or uncertainty_m < 0 or not np.isfinite(np.r_[start, end, extent, uncertainty_m]).all()):
        raise ValueError("invalid swept bound")
    from itertools import product
    offsets=np.array(list(product(*zip(-extent,extent))))
    return _points_clear(np.concatenate([start+offsets,end+offsets]),uncertainty_m,obstacles)


def insertion_depth_reached(object_z_m: float, insertion_depth_m: float, *, tolerance_m: float = 0.) -> bool:
    """Measured insertion against an explicit depth band, not command success.

    Default preserves the strict historical contract (1 micrometre numerical
    equality). Coarse tasks may bind a nonzero band to their registered motion
    goal tolerance. It must remain smaller than the requested insertion depth.
    XY, orientation, visible object and matching hole evidence are separate.
    """
    import math
    if not (all(type(v) in (int,float) and math.isfinite(v)
                for v in (object_z_m,insertion_depth_m,tolerance_m))
            and insertion_depth_m>0 and 0<=tolerance_m<insertion_depth_m):
        return False
    boundary=-insertion_depth_m+tolerance_m
    return object_z_m<=boundary or math.isclose(object_z_m,boundary,rel_tol=0.,abs_tol=1e-6)
