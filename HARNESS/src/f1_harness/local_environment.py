"""Current local collision evidence for world-frame observation movements.

Object pose may be absent. Possible target occupancy is refined by current
depth or measured surface constraints. With a current independently measured
fixture, registered table/bucket/native collision models govern the rest of
this single-object scene, exactly as for a full pose. This is a coarse model
contract, not proof that unregistered obstacles do not exist.
"""
from dataclasses import replace
from itertools import product
from functools import lru_cache
import math
import numpy as np
from scipy.ndimage import maximum_filter

from .contracts import HarnessError
from .geometry import transform


@lru_cache(maxsize=8)
def _fine_depth(payload,sha256,width,height):
    import base64,hashlib,zlib
    count=width*height*4
    if count>32*1024**2:raise HarnessError('local_fine_depth_size_invalid')
    decoder=zlib.decompressobj()
    raw=decoder.decompress(base64.b64decode(payload,validate=True),count+1)
    if (len(raw)!=count or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or hashlib.sha256(raw).hexdigest()!=sha256):
        raise HarnessError('local_fine_depth_identity_invalid')
    depth=np.frombuffer(raw,dtype='<f4').reshape(height,width)
    if not np.isfinite(depth).all() or np.any(depth<0):
        raise HarnessError('local_fine_depth_values_invalid')
    return depth


def local_scope(observation):
    return (observation.geometry_scope=='coarse_local_environment'
        or observation.geometry_scope=='coarse_object_verification' and
            (observation.local_collision_evidence is not None
             or getattr(observation,'local_collision_evidence_ref',None) is not None))


def require_local_evidence(task,observation,fixture=None):
    evidence=observation.local_collision_evidence
    target=next((o for o in observation.objects if o.object_id==task.object_id),None)
    if (not local_scope(observation) or task.admission_profile!='development_coarse'
            or task.goal!='placed' or evidence is None or target is None or not target.visible
            or observation.geometry_scope=='coarse_local_environment' and target.pose is not None
            or target.held is True or target.eef_object_pose is not None
            or not target.evidence_ids or evidence.object_id!=task.object_id
            or evidence.observation_id!=observation.observation_id
            or evidence.calibration_id!=observation.calibration_id
            or evidence.calibration_id!=task.calibration_id
            or evidence.measured_at_ns!=observation.received_ns
            or evidence.state_received_ns!=observation.state_received_ns
            or target.frame_id!=evidence.frame_id):
        raise HarnessError('local_environment_binding_mismatch')
    frames={(f.camera,f.stream):f for f in observation.frames}
    if any(frames.get((f.camera,f.stream))!=f for f in evidence.source_frames):
        raise HarnessError('local_environment_frame_identity_changed')
    for view in evidence.free_space_views:
        source={(f.camera,f.stream):f for f in evidence.source_frames}
        if any(source.get((view.camera,stream)) is None or
               source[(view.camera,stream)].blob_sha256!=value
               for stream,value in (('rgb',view.source_rgb_sha256),('depth',view.source_depth_sha256))):
            raise HarnessError('local_environment_depth_source_missing')
        transform(view.camera_pose_world)
        k=np.asarray(view.intrinsics).reshape(3,3)
        if (k[0,0]<=0 or k[1,1]<=0 or not np.allclose(k[2],[0,0,1],rtol=0,atol=1e-9)):
            raise HarnessError('local_environment_intrinsics_invalid')
    if fixture is not None:
        box=fixture.get('object_local_box')
        diagonal=(float(np.linalg.norm(np.asarray(box['max'])-np.asarray(box['min']))) if box else None)
        if (box is None or evidence.frame_id!=fixture['native_frame']
                or not math.isclose(diagonal,evidence.registered_object_diagonal_m,abs_tol=1e-9)
                or not math.isclose(evidence.planning_margin_m,fixture['planning_margin_m'],abs_tol=1e-9)
                or not fixture.get('independent_fixture_localization')):
            raise HarnessError('local_environment_registration_missing')
    return evidence


def require_open_tool(observation,fixture,held=None):
    from .gripper_state import observed_open
    upper=fixture.get('release_opening',0)+fixture.get('gripper_tolerance',0)
    if (observation.state16 is None or observation.state_valid_mask is None
            or not observation.state_valid_mask[15] or not observed_open(observation.state16[15],[0.,upper])
            or held is not None and not observed_open(held[15],[0.,upper])):
        raise HarnessError('local_observation_motion_requires_measured_open_gripper')


def target_excluded(evidence,low,high,*,margin_m,object_pose=None,object_local_box=None):
    """Conservative cells outside the current registered target model."""
    low=np.asarray(low).reshape(-1,3);high=np.asarray(high).reshape(-1,3)
    if object_pose is not None and object_local_box is not None:
        signs=np.array(list(product((-1.,1.),repeat=3)))
        points=(low[:,None,:]+high[:,None,:])/2+signs*(high[:,None,:]-low[:,None,:])/2
        inverse=np.linalg.inv(transform(object_pose))
        local=points@inverse[:3,:3].T+inverse[:3,3]
        return np.any((local.max(1)<np.asarray(object_local_box['min'])-margin_m)
            |(local.min(1)>np.asarray(object_local_box['max'])+margin_m),axis=1)
    # The registered diameter bounds unknown physical volume. Additional tool
    # padding follows the very same policy as an ordinary complete scene.
    radius=evidence.registered_object_diagonal_m+margin_m
    anchor=evidence.surface_anchor
    bounds_min=np.asarray(anchor.bounds_max if anchor else evidence.observed_surface_min)-radius
    bounds_max=np.asarray(anchor.bounds_min if anchor else evidence.observed_surface_max)+radius
    outside=np.any((high<bounds_min)|(low>bounds_max),axis=1)
    if anchor is not None and anchor.extremal_points_world:
        points=np.asarray(anchor.extremal_points_world)
        distance=np.linalg.norm(np.maximum(np.maximum(low[:,None,:]-points,points-high[:,None,:]),0),axis=2)
        outside|=np.any(distance>radius,axis=1)
    if anchor is not None and anchor.supporting_plane is not None:
        plane=anchor.supporting_plane;n=np.asarray(plane.normal_world)
        # Registered convex solid: all of it lies behind this independently
        # checked outward face, including unseen backs. Keep the original
        # measured front-side extent; fitting does not create an object frame.
        nearest=np.where(n>=0,low,high)
        outside|=((nearest-np.asarray(plane.centre_world))@n>max(0.,plane.all_target_front_side_max_m)+margin_m)
    return outside


def departure_plane(evidence):
    anchor=evidence.surface_anchor
    if anchor is None or anchor.supporting_plane is None:return None
    plane=anchor.supporting_plane;n=np.asarray(plane.normal_world)
    return dict(type='halfspace',role='target_object',object_id=evidence.object_id,
        normal=n.tolist(),point=(np.asarray(plane.centre_world)+n*max(0.,plane.all_target_front_side_max_m)).tolist())


def local_departure(scene,start,end):
    """The existing positive-clearance departure proof, with one fixed origin."""
    from .geometry import tool_margin_departure_clear
    from .skills import tool_margin
    margin=tool_margin(scene)
    if (margin<=0 or not scene.local_registered_environment or scene.base_object is not None
            or scene.cartesian_request is None):return None
    evidence=scene.local_collision_evidence;plane=departure_plane(evidence)
    if plane is None:return None
    native=transform(scene.diagnostic_native_from_world)
    reference=native@scene.base_eef
    goals=[native@transform(goal) for goal in scene.cartesian_request['goals']]
    goal=goals[-1]
    options=dict(margin_m=margin,local_boxes=scene.tool_local_boxes,
        target_object_id=evidence.object_id,departure_reference=reference,departure_goal=goal)
    before=reference
    for after in goals:
        if not tool_margin_departure_clear(before,after,scene.eef_tip_reference,scene.attached_radius_m,(plane,),**options):return None
        before=after
    if not tool_margin_departure_clear(native@transform(start),native@transform(end),scene.eef_tip_reference,
            scene.attached_radius_m,(plane,),**options):return None
    return dict(reference=reference.tolist(),goal=goal.tolist(),start=(native@transform(start)).tolist(),
                end=(native@transform(end)).tolist())


def require_departure_endpoint(scene,end):
    from .geometry import tool_sweep_clear
    from .skills import tool_margin
    if getattr(scene,'local_collision_evidence',None) is None or scene.cartesian_request is None:return
    goal=scene.cartesian_request['goals'][-1]
    if local_departure(scene,goal,goal) is None:return
    plane=departure_plane(scene.local_collision_evidence)
    actual=transform(scene.diagnostic_native_from_world)@transform(end)
    if not tool_sweep_clear(actual,actual,scene.eef_tip_reference,scene.attached_radius_m,(plane,),
            margin_m=tool_margin(scene),local_boxes=scene.tool_local_boxes):
        raise HarnessError('local_observation_departure_incomplete')


def build_local_scene(service,obs,full,held,*,base_eef=None,motion=True):
    from .skills import SkillGeometry
    from .support_motion import scene_support
    fixture=service.fixture
    evidence=require_local_evidence(service.task,obs,fixture)
    target=next(obj for obj in obs.objects if obj.object_id==service.task.object_id)
    measured_target=None if target.pose is None else service.frames.world_pose(target.pose)
    if motion:require_open_tool(obs,fixture,held)
    reference=fixture.get('fixture_reference_hole')
    if obs.fixture is not None and (obs.fixture.frame_id!=fixture['native_frame'] or reference is None
            or not np.allclose(transform(obs.fixture.reference_pose),transform(reference),rtol=0,atol=1e-9)):
        raise HarnessError('fixture_reference_identity_changed')
    if base_eef is None:base_eef=service.numerics.call('forward_pose',full22=full,held16=held)
    moved=(None if obs.fixture is None else
        service.frames.world_pose(obs.fixture.pose)@np.linalg.inv(service.frames.world_pose(reference)))
    obstacles=[]
    for obstacle in service.frames.world_obstacles(fixture['obstacles']):
        if obstacle.get('role')=='target_object' and obstacle.get('object_id')==service.task.object_id:
            # Its old pose is not evidence. Current possible occupancy remains
            # in the evidence object and is refined by observed space below.
            if measured_target is None:continue
            obstacle=dict(obstacle,**fixture['object_local_box'],pose=measured_target.tolist())
        if obstacle.get('role')=='bucket' and moved is not None:
            obstacle=dict(obstacle,pose=(moved@transform(obstacle['pose'])).tolist())
        obstacles.append(obstacle)
    if not any(o.get('role')=='table' and o.get('type')=='halfspace' for o in obstacles):
        raise HarnessError('local_observation_registered_support_missing')
    if measured_target is not None and not any(o.get('role')=='target_object' for o in obstacles):
        obstacles.append(dict(role='target_object',object_id=service.task.object_id,
            **fixture['object_local_box'],pose=measured_target.tolist()))
    return SkillGeometry(base_eef=transform(base_eef),base_object=measured_target,base_hole=None,
        eef_object=None,object_grasp_eef=None,obstacles=tuple(obstacles),
        **{key:fixture[key] for key in ('approach_clearance_m','transfer_clearance_m','insertion_depth_m',
            'attached_radius_m','effective_clearance_m','grasp_opening','release_opening')},
        uncertainty_m=None,goal=service.task.goal,eef_tip_reference=fixture['eef_tip_reference'],
        tool_local_boxes=fixture['tool_local_boxes'],admission_profile='development_coarse',
        planning_margin_m=fixture['planning_margin_m'],object_planning_margin_m=fixture['object_planning_margin_m'],
        object_local_box=fixture['object_local_box'] if measured_target is not None else None,
        object_held=target.held,target_object_id=service.task.object_id,
        aperture_measured=False,tool_padding_policy=fixture.get('tool_padding_policy','required'),
        diagnostic_native_from_world=np.linalg.inv(service.frames.world_native),
        support_measurement=scene_support(service,obs),local_collision_evidence=evidence,
        local_registered_environment=obs.fixture is not None)


def context(scene,observation,*,task=None,fixture=None):
    from .geometry_context import POSE_FORMAT,tool_geometry,entity_status
    from .skills import tool_margin
    evidence=scene.local_collision_evidence
    target=next((obj for obj in observation.objects if obj.object_id==evidence.object_id),None)
    object_pose=None if target is None or target.pose is None else list(target.pose)
    available=['reobserve']
    try:
        require_open_tool(observation,fixture or dict(release_opening=scene.release_opening,gripper_tolerance=0))
        available.append('move_tcp')
    except HarnessError:pass
    native=transform(scene.diagnostic_native_from_world)
    obstacles=[]
    for o in scene.obstacles:
        if o.get('type')=='halfspace':
            o=dict(o,point=(native[:3,:3]@np.asarray(o['point'])+native[:3,3]).tolist(),
                   normal=(native[:3,:3]@np.asarray(o['normal'])).tolist())
        elif 'pose' in o:o=dict(o,pose=(native@transform(o['pose'])).tolist())
        obstacles.append(o)
    return dict(schema='f1-geometric-tools-v1',geometry_scope=observation.geometry_scope,
        geometry_status='partial',pose_format=POSE_FORMAT,world_frame=evidence.frame_id,
        current_tcp_pose=(native@scene.base_eef@transform(scene.eef_tip_reference)).ravel().tolist(),
        object_pose=object_pose,hole_pose=None,tool_geometry=tool_geometry(scene),obstacles=obstacles,
        local_environment=dict(observed_surface_aabb=dict(min=evidence.observed_surface_min,max=evidence.observed_surface_max),
            possible_target_occupancy_aabb=dict(min=evidence.possible_occupancy_min,max=evidence.possible_occupancy_max),
            supporting_plane=(None if evidence.surface_anchor is None or evidence.surface_anchor.supporting_plane is None else dict(
                normal_world=evidence.surface_anchor.supporting_plane.normal_world,
                offset_m=float(np.dot(evidence.surface_anchor.supporting_plane.normal_world,
                                      evidence.surface_anchor.supporting_plane.centre_world)),
                source='current_independently_fitted_outward_face_of_registered_convex_box',
                measured_front_side_extent_m=max(0.,evidence.surface_anchor.supporting_plane.all_target_front_side_max_m),
                outside_target_when='normal_dot_point > offset + measured_front_side_extent_m + effective_tool_margin_m',
                effective_tool_margin_m=tool_margin(scene),tool_padding_policy=scene.tool_padding_policy,
                configured_tool_margin_m=evidence.planning_margin_m)),
            environment_model=dict(scope='registered_single_object_scene',
                current_fixture_available=scene.local_registered_environment,
                target_model='measured_registered_box' if object_pose is not None else 'measured_surface_constraints_and_registered_diameter',
                unregistered_obstacles_supported=False,physical_accuracy_certified=False),
            coverage=[dict(camera=v.camera,valid_tiles=sum(d>0 for d in v.min_depth_m),
                total_tiles=len(v.min_depth_m),near_depth_m=v.near_depth_m) for v in evidence.free_space_views],
            unknown_space_is_free=False,pose_available=object_pose is not None,source_observation_id=evidence.observation_id),
        **({} if task is None else {k:v for k,v in entity_status(task,observation).items()
            if k in ('entities','missing_entities','perception_boundary','quality_warnings')}),
        available_skills=available,
        cartesian_motion=dict(target_frames=['world'],support_contact=False,
            requires='fresh_open_gripper_and_current_observed_sweep'),motion_authorized=False,
        observation_id=observation.observation_id,calibration_id=observation.calibration_id)


def local_sweep_evidence(scene,start,end,*,measurement_envelope=False):
    """Cover conservative swept tool cells using measured rays or occupied space.

    Recursion resolves coarse box/view boundary overlap. An exhausted numerical
    cover budget reports unknown space, never a collision or a low confidence.
    """
    from scipy.spatial.transform import Rotation
    from .skills import tool_margin
    evidence=scene.local_collision_evidence
    margin=tool_margin(scene)
    a,b=map(transform,(start,end));native=transform(scene.diagnostic_native_from_world)
    certificate=getattr(scene,'local_sweep_certificate',None)
    if certificate is not None:
        certificate['effective_tool_margin_m']=margin
        certificate['tool_padding_policy']=scene.tool_padding_policy
    departing=local_departure(scene,a,b)
    if departing is None and measurement_envelope:
        # A nominal/feedback discrepancy is an unoriented geometric envelope,
        # not a record that the robot moved backwards along that discrepancy.
        departing=local_departure(scene,b,a)
    if departing is not None:
        if certificate is not None:certificate['departures'].append(departing)
        return dict(clear=True,cells_tested=0,target_margin_departure=True,
            evidence_observation_id=evidence.observation_id,unknown_space_is_free=False)
    views=[]
    for view in evidence.free_space_views:
        views.append((view,np.linalg.inv(transform(view.camera_pose_world))@native,
            np.asarray(view.intrinsics).reshape(3,3),np.asarray(view.min_depth_m).reshape(view.grid_height,view.grid_width)))
    object_pose=None if scene.base_object is None else native@scene.base_object
    rotation=Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).as_rotvec()
    angle=float(np.linalg.norm(rotation))
    tested=0;maximum=8192;failure={}
    signs=np.array(list(product((-1.,1.),repeat=3)))

    def covered(points,padding):
        nonlocal failure
        native_points=points@native[:3,:3].T+native[:3,3]
        # Only the target differs from the ordinary registered full scene.
        # Outside its possible occupancy, the same current table/bucket/native
        # models apply; do not invent an additional all-pixels-visible gate.
        outside=bool(target_excluded(evidence,native_points.min(0)-padding,native_points.max(0)+padding,
            margin_m=margin,object_pose=object_pose,object_local_box=scene.object_local_box)[0])
        if outside and scene.local_registered_environment:
            if certificate is not None:
                certificate['occupied'].add(tuple(np.r_[native_points.min(0)-padding,
                                                         native_points.max(0)+padding]))
            return True
        reasons=[]
        for view,inverse,k,depth in views:
            camera=points@inverse[:3,:3].T+inverse[:3,3]
            low=camera.min(0)-padding;high=camera.max(0)+padding
            if low[2]<=view.near_depth_m:
                reasons.append(dict(camera=view.camera,reason='before_observed_depth_range',z_min=float(low[2])));continue
            hull=(low+high)/2+signs*(high-low)/2
            projected=hull@k.T;uv=projected[:,:2]/projected[:,2,None]
            lower=np.floor(uv.min(0)).astype(int);upper=np.ceil(uv.max(0)).astype(int)
            if np.any(lower<0) or upper[0]>=view.image_width or upper[1]>=view.image_height:
                reasons.append(dict(camera=view.camera,reason='outside_observed_image',pixel_min=lower.tolist(),pixel_max=upper.tolist()));continue
            first=lower//view.tile_size_px;last=upper//view.tile_size_px
            values=depth[first[1]:last[1]+1,first[0]:last[0]+1]
            if view.fine_depth_zlib_base64 is not None and (
                    np.any(values<=0) or high[2]>=float(values.min())-margin):
                fine=_fine_depth(view.fine_depth_zlib_base64,view.fine_depth_sha256,view.image_width,view.image_height)
                values=fine[lower[1]:upper[1]+1,lower[0]:upper[0]+1]
            if np.any(values<=0):
                reasons.append(dict(camera=view.camera,reason='depth_pixels_unknown',unknown_pixels=int(np.sum(values<=0))));continue
            minimum=float(values.min())
            if high[2]<minimum-margin:
                if certificate is not None:
                    required=certificate['views'].setdefault(view.camera,np.zeros(
                        (view.image_height,view.image_width),dtype=np.float32))
                    bounds=certificate['view_bounds'].setdefault(view.camera,[low.copy(),high.copy()])
                    bounds[0]=np.minimum(bounds[0],low);bounds[1]=np.maximum(bounds[1],high)
                    selected=required[lower[1]:upper[1]+1,lower[0]:upper[0]+1]
                    np.maximum(selected,np.nextafter(np.float32(high[2]+margin),np.float32(np.inf)),out=selected)
                return True
            reasons.append(dict(camera=view.camera,reason='depth_surface_or_occlusion',
                z_max=float(high[2]),nearest_depth_m=minimum,margin_m=margin))
        failure=dict(cell_min=native_points.min(0).tolist(),cell_max=native_points.max(0).tolist(),
                     possible_target_overlap=not outside,views=reasons)
        return False

    def interval(low,high,offset,t0,t1,depth=0):
        nonlocal tested
        tested+=1
        if tested>maximum:return False
        corners=(low+high)/2+signs*(high-low)/2
        corners=corners@offset[:3,:3].T+offset[:3,3]
        radius=float(np.max(np.linalg.norm(corners,axis=1)))
        positions=[]
        for t in (t0,t1):
            r=a[:3,:3]@Rotation.from_rotvec(t*rotation).as_matrix()
            p=a[:3,3]+t*(b[:3,3]-a[:3,3])
            positions.append(corners@r.T+p)
        padding=radius*(angle*(t1-t0))**2/8
        if covered(np.concatenate(positions),padding):return True
        if depth>=18:return False
        size=high-low;translation=float(np.linalg.norm(b[:3,3]-a[:3,3]))*(t1-t0)
        rotational=radius*angle*(t1-t0)
        if max(translation,rotational)>float(size.max()):
            middle=(t0+t1)/2
            return interval(low,high,offset,t0,middle,depth+1) and interval(low,high,offset,middle,t1,depth+1)
        axis=int(np.argmax(size));middle=(low[axis]+high[axis])/2
        left=high.copy();left[axis]=middle;right=low.copy();right[axis]=middle
        return interval(low,left,offset,t0,t1,depth+1) and interval(right,high,offset,t0,t1,depth+1)

    clear=all(interval(np.asarray(box['min']),np.asarray(box['max']),transform(box.get('pose',np.eye(4))),0.,1.)
              for box in scene.tool_local_boxes)
    return dict(clear=bool(clear),cells_tested=tested,refinement_budget_exhausted=tested>maximum,
        evidence_observation_id=evidence.observation_id,unknown_space_is_free=False,
        **({} if clear else failure))


def seal_sweep_certificate(certificate,observation):
    """Worker-only compression; owner consumes a bounded vector comparison."""
    import base64,hashlib,zlib
    views=[]
    for camera,required in certificate['views'].items():
        raw=required.astype('<f4').tobytes()
        views.append(dict(camera=camera,width=required.shape[1],height=required.shape[0],
            required_zlib_base64=base64.b64encode(zlib.compress(raw)).decode(),
            required_sha256=hashlib.sha256(raw).hexdigest(),pixels=int(np.count_nonzero(required)),
            camera_bounds=[bound.tolist() for bound in certificate['view_bounds'][camera]],
            pixel_bounds=[int(np.where(required>0)[1].min()),int(np.where(required>0)[0].min()),
                          int(np.where(required>0)[1].max()),int(np.where(required>0)[0].max())]))
    return dict(schema='f1-local-swept-space-v1',observation_id=observation.observation_id,
        static_scope='registered_single_object_scene',views=views,
        target_excluded_cells=[list(row) for row in sorted(certificate['occupied'])],
        object_local_box=certificate['object_local_box'],
        effective_tool_margin_m=certificate['effective_tool_margin_m'],tool_padding_policy=certificate['tool_padding_policy'],
        target_motion_tolerance_m=certificate['target_motion_tolerance_m'],
        target_rotation_tolerance_rad=certificate['target_rotation_tolerance_rad'],
        departures=certificate['departures'],
        tool_local_boxes=[{key:np.asarray(value).tolist() if key in ('pose','min','max') else value
                          for key,value in box.items()} for box in certificate['tool_local_boxes']],
        eef_tip_reference=transform(certificate['eef_tip_reference']).tolist(),attached_radius_m=certificate['attached_radius_m'],
        retention='Original measured corridor only; later depth holes are quality loss, never new free space.')


def check_sweep_certificate(certificate,original,latest):
    """No FK, recursion, new free space or source-clock rewriting on the owner."""
    from .revalidation import RevalidationEvidenceUpdated
    if (not isinstance(certificate,dict) or certificate.get('schema')!='f1-local-swept-space-v1'
            or certificate.get('observation_id')!=original.observation_id
            or certificate.get('static_scope')!='registered_single_object_scene'):
        raise HarnessError('local_environment_sweep_certificate_invalid')
    before=original.local_collision_evidence;after=latest.local_collision_evidence
    if before is None or after is None:
        raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
    margin=certificate.get('effective_tool_margin_m');policy=certificate.get('tool_padding_policy')
    if (policy not in ('required','advisory') or margin is None or not math.isfinite(margin) or margin<0
            or policy=='advisory' and margin!=0
            or policy=='required' and margin!=before.planning_margin_m):
        raise HarnessError('local_environment_sweep_certificate_invalid')
    old={v.camera:v for v in before.free_space_views};new={v.camera:v for v in after.free_space_views}
    if certificate.get('departures'):
        from .geometry import tool_margin_departure_clear
        plane=departure_plane(after)
        if plane is None or original.fixture is None or latest.fixture is None:
            raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
        for segment in certificate['departures']:
            if not tool_margin_departure_clear(segment['start'],segment['end'],certificate['eef_tip_reference'],
                    certificate['attached_radius_m'],(plane,),margin_m=margin,
                    local_boxes=certificate['tool_local_boxes'],target_object_id=after.object_id,
                    departure_reference=segment['reference'],departure_goal=segment['goal']):
                raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
    if certificate['views']:
        # A hole in a later image is only a quality loss while the registered
        # target is unchanged under the existing scene-motion tolerance. It
        # must not hide affirmative evidence that the target itself moved.
        def measured_bounds(e):
            return np.asarray((e.surface_anchor.bounds_min,e.surface_anchor.bounds_max)
                if e.surface_anchor is not None else (e.observed_surface_min,e.observed_surface_max))
        if np.max(np.linalg.norm(measured_bounds(before)-measured_bounds(after),axis=1))>certificate['target_motion_tolerance_m']:
            raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
        old_target=next(obj for obj in original.objects if obj.object_id==before.object_id)
        new_target=next(obj for obj in latest.objects if obj.object_id==after.object_id)
        if old_target.pose is not None and new_target.pose is not None:
            from .revalidation import pose_error
            distance,angle=pose_error(old_target.pose,new_target.pose)
            if distance>certificate['target_motion_tolerance_m'] or angle>certificate['target_rotation_tolerance_rad']:
                raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
        elif (before.surface_anchor is not None and after.surface_anchor is not None
                and before.surface_anchor.supporting_plane is not None and after.surface_anchor.supporting_plane is not None):
            angle=float(np.arccos(np.clip(np.dot(before.surface_anchor.supporting_plane.normal_world,
                                               after.surface_anchor.supporting_plane.normal_world),-1,1)))
            if angle>certificate['target_rotation_tolerance_rad']:
                raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
    missing=0;checked=0
    for item in certificate['views']:
        camera=item['camera'];a=old.get(camera);b=new.get(camera)
        geometry=('intrinsics','image_width','image_height','near_depth_m')
        if a is None or b is None or any(getattr(a,key)!=getattr(b,key) for key in geometry):
            raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
        if (item['width'],item['height'])!=(a.image_width,a.image_height):
            raise HarnessError('local_environment_sweep_certificate_invalid')
        required=_fine_depth(item['required_zlib_base64'],item['required_sha256'],item['width'],item['height'])
        x0,y0,x1,y1=item['pixel_bounds'];depth_shift=0.
        if a.camera_pose_world!=b.camera_pose_world:
            changed=np.linalg.inv(transform(b.camera_pose_world))@transform(a.camera_pose_world)
            bounds=np.asarray(item['camera_bounds']);zmin=float(bounds[0,2])
            radius=float(np.linalg.norm(np.maximum(abs(bounds[0]),abs(bounds[1]))))
            # Operator-norm rotation displacement gives a uniform metric
            # bound over every proven point. Projection is dilated by its
            # derived pixel displacement, not an arbitrary pose-noise cutoff.
            depth_shift=float(np.linalg.norm(changed[:3,3])+np.linalg.norm(changed[:3,:3]-np.eye(3),2)*radius)
            if zmin<=depth_shift:
                raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
            extent=np.maximum(abs(bounds[0,:2]),abs(bounds[1,:2]));k=np.asarray(a.intrinsics).reshape(3,3)
            shifts=np.ceil(np.diag(k)[:2]*depth_shift*(1+extent/zmin)/(zmin-depth_shift)).astype(int)
            rx=min(int(shifts[0]),a.image_width);ry=min(int(shifts[1]),a.image_height)
            if x0-rx<0 or y0-ry<0 or x1+rx>=a.image_width or y1+ry>=a.image_height:
                raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
            x0=max(0,x0-rx);x1=min(a.image_width-1,x1+rx)
            y0=max(0,y0-ry);y1=min(a.image_height-1,y1+ry)
            required=maximum_filter(required[y0:y1+1,x0:x1+1],size=(2*ry+1,2*rx+1),mode='constant',cval=0)
        else:required=required[y0:y1+1,x0:x1+1]
        if b.fine_depth_zlib_base64 is not None:
            current=_fine_depth(b.fine_depth_zlib_base64,b.fine_depth_sha256,b.image_width,b.image_height)
        else:
            grid=np.asarray(b.min_depth_m).reshape(b.grid_height,b.grid_width)
            current=np.repeat(np.repeat(grid,b.tile_size_px,axis=0),b.tile_size_px,axis=1)[:b.image_height,:b.image_width]
        current=current[y0:y1+1,x0:x1+1]
        needed=required>0;valid=current>0
        checked+=int(np.count_nonzero(needed));missing+=int(np.count_nonzero(needed&~valid))
        if np.any(needed&valid&(current<=required+depth_shift)):
            raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
    cells=np.asarray(certificate['target_excluded_cells'],float)
    if cells.size:
        if original.fixture is None or latest.fixture is None:
            raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
        if cells.ndim!=2 or cells.shape[1]!=6 or not np.isfinite(cells).all():
            raise HarnessError('local_environment_sweep_certificate_invalid')
        obj=next(obj for obj in latest.objects if obj.object_id==after.object_id)
        outside=target_excluded(after,cells[:,:3],cells[:,3:],margin_m=margin,object_pose=obj.pose,
                                object_local_box=certificate['object_local_box'])
        if not np.all(outside):raise RevalidationEvidenceUpdated('revalidation_local_evidence_updated')
    return dict(checked_pixels=checked,retained_original_depth_pixels=missing,
                original_observation_id=original.observation_id,latest_observation_id=latest.observation_id)
