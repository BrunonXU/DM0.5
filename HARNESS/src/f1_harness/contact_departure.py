"""A registered empty-tool contact exit, not a general obstacle exemption."""
from itertools import product
import numpy as np
from scipy.spatial.transform import Rotation
from .geometry import transform, tool_sweep_clear


def _corners(box):
    low, high = np.asarray(box['min'], float), np.asarray(box['max'], float)
    if low.shape != (3,) or high.shape != (3,) or not np.isfinite([low, high]).all() or np.any(low >= high):
        raise ValueError('invalid_departure_box')
    frame = transform(box.get('pose', np.eye(4)))
    return np.array(list(product(*zip(low, high)))) @ frame[:3,:3].T + frame[:3,3], frame[:3,:3].T


def _contact_axes(box,pose,other,other_axes):
    corners,local_axes=_corners(box)
    world=corners@pose[:3,:3].T+pose[:3,3]
    axes=local_axes@pose[:3,:3].T
    axes=np.concatenate((axes,other_axes,np.cross(axes[:,None,:],other_axes[None,:,:]).reshape(-1,3)))
    axes=axes[np.linalg.norm(axes,axis=1)>1e-10]
    axes/=np.linalg.norm(axes,axis=1)[:,None]
    axes=np.concatenate((axes,-axes))
    gap=(world@axes.T).min(0)-(other@axes.T).max(0)
    return corners,axes,gap


def outward_contact_direction(scene):
    """Propose departure toward the nearest contact faces, never certify it.

    Ordinary and actual-IK dual-hypothesis sweeps still decide acceptability.
    A vertical tangent can fail after IK changes orientation slightly; this
    provides a distinct outward path rather than relaxing the contact proof.
    """
    target=[o for o in scene.obstacles if o.get('role')=='target_object'
            and o.get('object_id')==scene.target_object_id]
    if len(target)!=1:return None
    other,other_axes=_corners(target[0]);current=transform(scene.base_eef)
    up=transform(scene.base_object)[:3,2];directions=[]
    for box in scene.tool_local_boxes or ():
        _,axes,gap=_contact_axes(box,current,other,other_axes)
        if np.max(gap)>0:continue
        closest=axes[gap>=np.max(gap)-1e-10]
        directions.append(closest[np.argmax(closest@up)])
    if not directions:return None
    direction=np.sum(directions,axis=0);norm=np.linalg.norm(direction)
    return direction/norm if norm>1e-10 else None


def target_departure_clear(scene, poses):
    """Prove exit from expected target contact after the empty gripper opens.

    For each initially overlapping tool box, use only its closest SAT face(s).
    Every segment must increase that signed clearance, with a rotation bound
    for every corner. Picking an arbitrary opposite face would allow passing
    through the object, so it is forbidden. Non-target obstacles and final full
    target clearance use the ordinary sweep. Caller must bind this operation to
    an opened, observed-empty regrasp stage; the function grants no authority.
    """
    if scene.object_held is not False or scene.eef_object is not None:
        return False
    return contact_exit_geometry_clear(scene,poses)


def contact_exit_geometry_clear(scene, poses):
    """Pure empty-target hypothesis; does not declare the real gripper empty.

    An uncertain-grasp observation must ALSO check its loaded hypothesis and
    bind the operation, gripper and authority. No contact privilege survives
    the final full-clearance endpoint.
    """
    if not scene.target_object_id or not scene.tool_local_boxes or len(poses)<2:return False
    from .skills import tool_margin
    path = [transform(p) for p in poses]
    target = [o for o in scene.obstacles if o.get('role') == 'target_object'
              and o.get('object_id') == scene.target_object_id]
    if len(target) != 1 or target[0].get('type') == 'halfspace':
        return False
    remaining = [o for o in scene.obstacles if o is not target[0]]
    margin = tool_margin(scene)
    def clear(a,b,obstacles):
        return tool_sweep_clear(a,b,scene.eef_tip_reference,scene.attached_radius_m,obstacles,
                               margin_m=margin,local_boxes=scene.tool_local_boxes)
    if not clear(path[-1],path[-1],scene.obstacles):
        return False
    if not all(clear(a,b,remaining) for a,b in zip(path,path[1:])):
        return False
    other, other_axes = _corners(target[0])
    for box in scene.tool_local_boxes:
        corners,axes,gap=_contact_axes(box,path[0],other,other_axes)
        # Separated boxes can retain any existing separating face; contact
        # boxes must depart by a nearest face, never through the far side.
        allowed = gap > 0 if np.max(gap)>0 else gap >= np.max(gap)-1e-10
        radius = float(np.max(np.linalg.norm(corners,axis=1)))
        for a,b in zip(path,path[1:]):
            # Once ordinary clearance is restored, ordinary swept geometry
            # prevents re-entry; no contact privilege survives this segment.
            if tool_sweep_clear(a,b,scene.eef_tip_reference,scene.attached_radius_m,target,
                                margin_m=margin,local_boxes=(box,)):
                continue
            angle = float(np.linalg.norm(Rotation.from_matrix(a[:3,:3].T @ b[:3,:3]).as_rotvec()))
            allowed &= axes @ (b[:3,3]-a[:3,3]) >= angle*radius-1e-12
        if not np.any(allowed):
            return False
    return True
