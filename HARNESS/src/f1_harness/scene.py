"""Calibrated mask-to-scene conversion with explicit freshness and identity invalidation."""
import numpy as np

from .contracts import SceneObject, digest
from .geometry import transform, crop_intrinsics, masked_points, fit_block_pose, HEAD_ROI


def object_from_mask(*, object_id, shape, aligned_depth_m, mask, k_rgb, base_camera,
                     calibration_error_m, evidence_ids, frame_id, roi=HEAD_ROI,
                     surface_selection=None, diagnostics=None):
    x0,y0,x1,y1=roi
    if mask.shape!=(y1-y0,x1-x0):raise ValueError('mask_not_in_original_ROI_pixels')
    if np.any(mask[0]) or np.any(mask[-1]) or np.any(mask[:,0]) or np.any(mask[:,-1]):
        raise ValueError('object_truncated_by_ROI')
    points=masked_points(aligned_depth_m[y0:y1,x0:x1],mask,crop_intrinsics(k_rgb,roi))
    normal_prior=None
    if surface_selection is not None:
        from .surface import select_upright_face
        surface_report={}
        if diagnostics is not None:diagnostics['surface_selection']=surface_report
        points,surface_report=select_upright_face(points,base_camera,surface_selection,diagnostics=surface_report)
        if surface_selection.get('pose_normal_prior')=='registered_upright':
            normal_prior=transform(base_camera)[:3,:3].T@np.asarray(surface_selection['normal_world'],float)
            if diagnostics is not None:diagnostics['pose_normal_prior']='registered_upright'
        elif surface_selection.get('pose_normal_prior') is not None:
            raise ValueError('unknown_pose_normal_prior')
    fitted=fit_block_pose(points,normal_prior=normal_prior)
    if diagnostics is not None:diagnostics['observed_face_fit']=fitted
    base_object=transform(base_camera)@transform(fitted['pose'])
    return SceneObject(object_id=object_id,shape=shape,pose=tuple(base_object.ravel()),
                       frame_id=frame_id,uncertainty_m=calibration_error_m+fitted['residual_p95_m'],
                       evidence_ids=tuple(evidence_ids))


def box_frame_from_face(face_pose, fitted, box):
    """Register an observed rectangular face to known box dimensions.

    The face normal is not necessarily the object's long axis. Match its two
    measured edge lengths to the registered dimensions, retaining the object's
    original local box/origin. This does not certify occluded edges or support.
    """
    from itertools import permutations
    face=transform(face_pose)
    low,high=np.asarray(box['min'],float),np.asarray(box['max'],float)
    size=high-low
    spans=np.asarray(fitted['boundary_fit']['robust_planar_spans_m'],float)
    if size.shape!=(3,) or spans.shape!=(2,) or np.any(size<=0) or not np.isfinite(spans).all():
        raise ValueError('registered_box_face_dimensions_required')
    # Stable tie-breaking keeps the established upright frame for a square top.
    axes=min(permutations(range(3)),key=lambda p:(float(np.linalg.norm(size[list(p[:2])]-spans)),
                                                p[2]!=2,p))
    i,j,k=axes
    rotation=np.zeros((3,3));rotation[:,i]=face[:3,0];rotation[:,j]=face[:3,1];rotation[:,k]=face[:3,2]
    if np.linalg.det(rotation)<0:rotation[:,i if i!=2 else j]*=-1
    if k!=2:
        dominant=int(np.argmax(np.abs(rotation[:,2])))
        if rotation[dominant,2]<0:
            rotation[:,2]*=-1;rotation[:,i if i!=2 else j]*=-1
    centre=face[:3,3]-face[:3,2]*size[k]/2
    obj=np.eye(4);obj[:3,:3]=rotation;obj[:3,3]=centre-rotation@((low+high)/2)
    return obj,dict(normal_object_axis=k,observed_spans_m=spans.tolist(),
                    registered_face_spans_m=size[list(axes[:2])].tolist(),
                    dimension_residual_m=float(np.linalg.norm(size[list(axes[:2])]-spans)),
                    normal_is_long_axis=k==2,physical_accuracy_certified=False)


class SceneCache:
    """Contact, moved neighbors and fixture changes invalidate affected estimates."""
    def __init__(self):self.objects={};self.revision=0
    def update(self,objects):
        self.objects={obj.object_id:obj for obj in objects};self.revision+=1
    def invalidate(self,object_ids,reason):
        for key in object_ids:
            if key in self.objects:
                obj=self.objects[key]
                self.objects[key]=obj.model_copy(update=dict(pose=None,eef_object_pose=None,
                                                            held=None,inserted=None,stationary=None))
        self.revision+=1
        return dict(reason=reason,scene_version=self.identity())
    def identity(self):return digest(dict(revision=self.revision,objects=[o.model_dump() for o in self.objects.values()]))


def infer_in_hand(objects,base_eefs,*,max_relative_error_m,minimum_views=3):
    """Independent measured poses must move with the gripper; a static visual overlap is insufficient."""
    if len(objects)<minimum_views or len(objects)!=len(base_eefs):raise ValueError('in_hand_views_missing')
    if any(o.pose is None or not o.evidence_ids for o in objects):raise ValueError('object_pose_evidence_missing')
    eefs=[transform(t) for t in base_eefs]
    if max(np.linalg.norm(t[:3,3]-eefs[0][:3,3]) for t in eefs)<2*max_relative_error_m:
        raise ValueError('insufficient_independent_motion_for_grasp_verification')
    relative=[np.linalg.inv(eef)@transform(obj.pose) for obj,eef in zip(objects,eefs)]
    if max(np.linalg.norm(t[:3,3]-relative[-1][:3,3]) for t in relative)>max_relative_error_m:
        raise ValueError('object_not_tracking_gripper')
    return objects[-1].model_copy(update=dict(held=True,eef_object_pose=tuple(relative[-1].ravel())))
