"""Cheap, soft ordering of already registered square symmetries; no new goals."""
import numpy as np
from .geometry import transform
from .skills import propose
from .contracts import HarnessError


def order_variants(skill,scene,variants,prior,*,camera=None,preferences=None):
    """Optional ordering advice cannot remove branches or admit an unsafe path.

    A complete explicit order replaces the heuristic, so do not calculate
    geometry scores that would immediately be discarded. Invalid advice falls
    back to the original heuristic; motion validation stays in PlanService.
    """
    from .skills import candidate_variants
    import logging
    warning=None
    if preferences is not None and not isinstance(preferences,dict):
        warning='candidate_order_mapping_invalid'
        preferred=None
    else:
        preferred=(preferences or {}).get(skill)
    if preferred is not None:
        registered=list(candidate_variants(skill))
        if (isinstance(preferred,list) and all(type(v) is int for v in preferred)
                and len(preferred)==len(registered) and set(preferred)==set(registered)):
            ordered=sorted(variants,key=preferred.index)
            return ordered,[dict(variant=v,registered_preference_index=preferred.index(v),
                                 ordering_source='configured_registered_order') for v in ordered]
        warning='candidate_order_permutation_invalid'
    ordered,rows=rank_variants(skill,scene,variants,prior,camera=camera)
    if warning:
        logging.getLogger(__name__).warning('%s: %s; using default candidate ranking',warning,skill)
        by_variant={r['variant']:r for r in rows}
        rows=[dict(by_variant.get(v,dict(variant=v)),ordering_warning=warning,
                   ordering_source='default_after_invalid_preference') for v in ordered]
    return ordered,rows


def angle(a,b):
    return float(np.arccos(np.clip((np.trace(a[:3,:3].T@b[:3,:3])-1)/2,-1,1)))


def rank_grasp_choices(scene,candidates,prior,*,camera=None,skill='approach_grasp'):
    """Score actual continuous geometry, not a variant before its pitch is applied.

    Visibility is a soft cost, never a new action veto. User/model supplied
    poses remain a single choice; the agent can inspect and replace them.
    """
    from .grasp_candidates import apply_descriptor
    rows=[]
    reference=(prior or {}).get('eef_poses',{}).get('grasp')
    for index,(descriptor,variant) in enumerate(candidates):
        row=dict(index=index,variant=variant,descriptor=descriptor,score=None,
                 ordering_source='continuous_geometry')
        try:
            candidate=scene if descriptor is None else apply_descriptor(scene,descriptor)
            proposal=propose(skill,candidate,variant=variant,_endpoint_only=skill=='approach_grasp')
            goals=[transform(g) for stage in proposal.get('stages',[]) for g in stage['goals']]
            end=goals[-1];current=transform(scene.base_eef)
            tip=transform(scene.eef_tip_reference)
            distance=float(np.linalg.norm((end@tip)[:3,3]-(current@tip)[:3,3]))
            rotation=angle(current,end)
            prior_error=0. if reference is None else angle(transform(reference),end)
            view=None;penalty=0.
            if camera is not None:
                view=pregrasp_wrist_view(candidate,end,camera)
                if not view['inside_image']:
                    penalty=2.
                    if 'pixel_min' in view:
                        size=np.asarray(camera['image_size'],float)
                        penalty+=float((np.maximum(-np.asarray(view['pixel_min']),0)/size+
                                        np.maximum(np.asarray(view['pixel_max'])-size,0)/size).sum())
            row.update(score=distance/max(scene.approach_clearance_m,.001)+rotation+prior_error+penalty,
                tcp_distance_m=distance,rotation_rad=rotation,historical_orientation_error_rad=prior_error,
                wrist_view=view)
        except HarnessError as exc:row['geometry_reason']=str(exc)[:300]
        rows.append(row)
    rows.sort(key=lambda r:(r['score'] is None,r['score'] or 0.,r['index']))
    return [candidates[r['index']] for r in rows],rows


def pregrasp_wrist_view(scene,endpoint,camera):
    """Project the complete measured target top face at the next observation pose.

    This checks calibrated field of view only; it cannot certify visibility
    through a finger or neighbor. The runtime still needs a fresh real mask.
    """
    from itertools import product
    if not isinstance(camera,dict) or scene.object_local_box is None:
        raise HarnessError('registered_wrist_view_required')
    k=np.asarray(camera['k_rgb'],float);size=np.asarray(camera['image_size'],float)
    if k.shape!=(3,3) or size.shape!=(2,) or not np.isfinite(k).all() or not np.isfinite(size).all() or np.any(size<=2):
        raise HarnessError('registered_wrist_view_required')
    box=scene.object_local_box
    corners=np.array([[x,y,box['max'][2],1.] for x,y in product(
        (box['min'][0],box['max'][0]),(box['min'][1],box['max'][1]))])
    camera_object=np.linalg.inv(transform(endpoint)@transform(camera['eef_camera']))@transform(scene.base_object)
    points=(corners@camera_object.T)[:,:3]
    if np.any(points[:,2]<=0):
        return dict(inside_image=False,reason='target_behind_camera',occlusion_verified=False)
    pixels=points@k.T;pixels=pixels[:,:2]/pixels[:,2:]
    inside=bool(np.all(pixels>0)&np.all(pixels<size-1))
    return dict(inside_image=inside,reason='top_face_in_image' if inside else 'top_face_outside_image',
                pixel_min=pixels.min(0).tolist(),pixel_max=pixels.max(0).tolist(),occlusion_verified=False)


def rank_variants(skill,scene,variants,prior,*,camera=None):
    if skill=='withdraw_reset':
        # Keep direct/lower routes when already clear. A high detour can be
        # replaced by a registered gradual-turn bend; this only orders attempts.
        # Every attempt still goes through native IK and full FK revalidation.
        try:
            legacy=propose(skill,scene,variant=0)['evidence']['variant']
        except HarnessError as exc:
            if not str(exc).startswith('return_route_sweep_blocked:'):raise
            legacy='blocked'
        order=[1,0,2,3] if legacy in ('above_ready','blocked') else [0,1,2,3]
        values=sorted(variants,key=order.index)
        return values,[dict(variant=v,registered_preference_index=order.index(v),
                            legacy_geometry_route=legacy) for v in values]
    reference=prior.get('eef_poses',{}).get(skill) if prior else None
    if reference is None:return list(variants),[]
    reference=transform(reference);ranked=[]
    for variant in variants:
        try:proposal=propose(skill,scene,variant=variant)
        except HarnessError as exc:
            ranked.append(dict(variant=variant,score=None,geometry_reason=str(exc)));continue
        goals=[transform(g) for stage in proposal.get('stages',[]) for g in stage['goals']]
        previous=transform(scene.base_eef);distance=rotation=0.
        for goal in goals:
            distance+=float(np.linalg.norm(goal[:3,3]-previous[:3,3]));rotation+=angle(previous,goal);previous=goal
        historical_angle=angle(reference,previous)
        view_penalty=0.;visible=None
        if camera is not None and goals:
            camera_pose=previous@transform(camera['eef_camera'])
            object_pose=previous@transform(scene.eef_object) if scene.object_held is True and scene.eef_object is not None else transform(scene.base_object)
            point=object_pose[:3,3].copy()
            if scene.object_local_box:
                center=(np.asarray(scene.object_local_box['min'])+scene.object_local_box['max'])/2
                point=(object_pose@np.r_[center,1])[:3]
            point=(np.linalg.inv(camera_pose)@np.r_[point,1])[:3]
            k=np.asarray(camera['k_rgb'],float);width,height=camera['image_size']
            if point[2]<=0:visible=False;view_penalty=2.
            else:
                pixel=k@point;pixel=pixel[:2]/pixel[2]
                normalized=np.abs((pixel-[width/2,height/2])/[width/2,height/2])
                visible=bool(np.max(normalized)<1)
                view_penalty=float(np.maximum(normalized-.8,0).sum())
        # Metres normalized by the existing approach clearance; angles are radians.
        # This ordering never admits a path or relaxes any physical threshold.
        score=distance/max(scene.approach_clearance_m,.001)+rotation+historical_angle+view_penalty
        ranked.append(dict(variant=variant,score=score,path_length_m=distance,
            rotation_rad=rotation,historical_orientation_error_rad=historical_angle,
            predicted_target_in_wrist_frame=visible,visibility_is_occlusion_unverified=True))
    ranked.sort(key=lambda r:(r['score'] is None,r['score'] if r['score'] is not None else 0,r['variant']))
    return [r['variant'] for r in ranked],ranked
