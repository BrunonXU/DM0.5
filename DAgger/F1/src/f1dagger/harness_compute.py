"""Numerical-only Harness path solver; reuse F1 constrained IK and collision gate."""
import time
import numpy as np
from .robot_io import collision_gate_from_spec, CandidateSafetyRejected
from .teleop import F1ArmTarget


def planning_deadline(scene, maximum_s, exhausted_code, *, started=None):
    """Use the caller's remaining budget without extending native limits."""
    budget = (scene or {}).get('planning_budget_s', maximum_s)
    if (isinstance(budget, bool) or not isinstance(budget, (int, float))
            or not np.isfinite(budget) or budget < 0):
        raise ValueError('numerical_planning_budget_invalid')
    if budget == 0:
        raise ValueError(exhausted_code)
    return (time.monotonic() if started is None else started) + min(budget, maximum_s)


def sweep_preserving_indices(poses, check_segment, check_joint_segment=None):
    """Retain dense IK vertices when shortcutting would change collision validity.

    The first pose is the already emitted anchor; returned indices exclude it.
    A Cartesian shortcut and the commanded joint interpolation are different
    curves. Check both when joint states are available, keeping original dense
    vertices if either shortcut fails. This does not change contact permissions.
    Unknown errors propagate rather than being treated as simplification failures.
    """
    def visit(start, end):
        try:
            check_segment(poses[start], poses[end])
            if check_joint_segment is not None:
                check_joint_segment(start, end)
        except (ValueError, CandidateSafetyRejected) as exc:
            if ((not isinstance(exc, CandidateSafetyRejected) and str(exc).split(':', 1)[0] not in (
                    'actual_IK_path_tool_sweep_blocked',
                    'actual_IK_path_scene_sweep_blocked',
                    'actual_IK_path_attached_object_sweep_blocked')) or end-start <= 1):
                raise
            middle = (start+end)//2
            return visit(start, middle)+visit(middle, end)
        return [end]
    return visit(0, len(poses)-1) if len(poses)>1 else []


def check_joint_interpolation(start, end, pose_at, check_segment):
    """Check the FK curve of a HAL-degree linear reference, not a pose chord.

    Use the existing output audit's 0.1 degree subdivision. This is numerical
    path sampling, not an arrival tolerance or a measured motion certificate.
    The same scene checker decides each interval; no padding is removed.
    """
    start=np.asarray(start,float);end=np.asarray(end,float)
    steps=max(1,int(np.ceil(np.max(np.abs(end[7:14]-start[7:14]))/.1)))
    before=pose_at(start)
    for fraction in np.linspace(0.,1.,steps+1)[1:]:
        after=pose_at(start+fraction*(end-start))
        check_segment(before,after)
        before=after


def wrist_center_posture(jacobian, q):
    """Soft redundancy target, never a joint limit or an executable command.

    Project wrist centering into the instantaneous Cartesian null space. The
    constrained QP and actual-FK sweep still decide every feasible increment.
    """
    j=np.asarray(jacobian,float);q=np.asarray(q,float)
    if j.shape!=(6,7) or q.shape!=(7,) or not np.isfinite(j).all() or not np.isfinite(q).all():
        raise ValueError('invalid_redundancy_input')
    delta=np.zeros(7);delta[5]=-q[5]
    return q+(np.eye(7)-np.linalg.pinv(j,rcond=1e-5)@j)@delta


def demonstrated_posture(jacobian,q,reference):
    """Use every demonstrated joint only in Cartesian redundancy directions."""
    j=np.asarray(jacobian,float);q=np.asarray(q,float);reference=np.asarray(reference,float)
    if j.shape!=(6,7) or q.shape!=(7,) or reference.shape!=(7,) or not np.isfinite(np.r_[j.ravel(),q,reference]).all():
        raise ValueError('invalid_demonstrated_posture_input')
    return q+(np.eye(7)-np.linalg.pinv(j,rcond=1e-5)@j)@(reference-q)


class CartesianProgress:
    """Optimizer convergence only: no waypoint admission or motion authority.

    Null-space joint movement does not prove that the current Cartesian target
    is getting closer. Accumulate improvements of 0.1% of the existing solver
    tolerances; give the same 25-iteration window as the joint-stall detector.
    A new interpolated waypoint starts a new window. Exhaustion rejects this
    numerical candidate, not the task or other poses/paths.
    """
    def __init__(self,position_tolerance,rotation_tolerance):
        self.resolution=np.array([position_tolerance,rotation_tolerance])*1e-3
        self.waypoint=None;self.best=None;self.stalled=0

    def update(self,waypoint,position_error,rotation_error):
        error=np.array([position_error,rotation_error])
        if waypoint!=self.waypoint:
            self.waypoint=waypoint;self.best=error;self.stalled=0
        elif np.any(self.best-error>self.resolution):
            self.best=np.minimum(self.best,error);self.stalled=0
        else:
            self.stalled+=1
        return self.stalled>=25


def scene_segment_checker(scene, initial_pose, pin):
    """One scene contract for Cartesian IK, joint search and path shortcuts."""
    from f1_harness.geometry import swept_box_clear,tool_sweep_clear,attached_object_sweep_clear
    def scene_clear(before,after):
        if scene is None:return
        if scene.get('tool_local_boxes'):
            if not tool_sweep_clear(before,after,scene['eef_tip_reference'],scene['attached_radius_m'],
                    scene['obstacles'],margin_m=scene['uncertainty_m'],local_boxes=scene['tool_local_boxes']):
                raise ValueError('actual_IK_path_tool_sweep_blocked')
        elif not swept_box_clear(before[:3,3],after[:3,3],[scene['attached_radius_m']]*3,
                                  scene['obstacles'],uncertainty_m=scene['uncertainty_m']):
            raise ValueError('actual_IK_path_scene_sweep_blocked')
        if scene.get('eef_object') is not None and scene.get('object_local_box') is not None:
            if not attached_object_sweep_clear(before,after,scene['eef_object'],scene['object_local_box'],
                    [o for o in scene['obstacles'] if o.get('collision_scope')!='tool_only'],
                    margin_m=scene['object_planning_margin_m'],
                    support_contact_tolerance_m=scene.get('support_contact_tolerance_m'),
                    support_contact_direction=scene.get('support_contact_direction','depart')):
                import json
                blocked=[o.get('id',o.get('role','unknown')) for o in scene['obstacles']
                    if o.get('collision_scope')!='tool_only' and not attached_object_sweep_clear(
                        before,after,scene['eef_object'],scene['object_local_box'],[o],
                        margin_m=scene['object_planning_margin_m'],
                        support_contact_tolerance_m=scene.get('support_contact_tolerance_m'),
                        support_contact_direction=scene.get('support_contact_direction','depart'))]
                raise ValueError('actual_IK_path_attached_object_sweep_blocked:'+json.dumps(dict(
                    obstacles=blocked,before=before.tolist(),after=after.tolist())))
        if 'maximum_approach_translation_m' in scene:
            distance=np.linalg.norm(after[:3,3]-initial_pose[:3,3])
            angle=np.linalg.norm(pin.log3(after[:3,:3]@initial_pose[:3,:3].T))
            if (distance>scene['maximum_approach_translation_m']
                    or angle>scene['maximum_approach_rotation_rad']):
                raise ValueError('actual_IK_path_development_increment_exceeded')
            if not scene.get('tool_local_boxes') and scene.get('eef_tip_reference') is not None:
                if not tool_sweep_clear(before,after,scene['eef_tip_reference'],scene['attached_radius_m'],
                                        scene['obstacles'],margin_m=scene['uncertainty_m']):
                    raise ValueError('actual_IK_path_tool_sweep_blocked')
    return scene_clear


class HarnessNumerics:
    def __init__(self,spec):
        self.gate=collision_gate_from_spec(spec['gate'])
        self.mapping=self.gate.mapping
        self.arm=F1ArmTarget(spec['urdf'],self.mapping,spec['ik'])
        try:
            from f1_harness.numerical_pool import NUMERICAL_RPC_TIMEOUT_S
            wall_cap=float(NUMERICAL_RPC_TIMEOUT_S)-2.0
        except ImportError:
            wall_cap=16.0
        max_wall_s=spec['ik'].get('max_wall_s',7.0)
        if (isinstance(max_wall_s,bool) or not isinstance(max_wall_s,(int,float))
                or not np.isfinite(max_wall_s) or not 0<float(max_wall_s)<=wall_cap):
            raise ValueError('invalid_ik_max_wall_s')
        self.max_wall_s=float(max_wall_s)
        self.position_tolerance=float(spec['position_tolerance_m'])
        self.rotation_tolerance=float(spec['rotation_tolerance_rad'])
        self.max_iterations=int(spec['max_iterations'])
        if not (0<self.position_tolerance<.01 and 0<self.rotation_tolerance<.1
                and 1<=self.max_iterations<=3000):raise ValueError('invalid measured solver tolerance')

    def forward_pose(self,*,full22,held16):
        self.arm.begin('right',np.asarray(full22,float),self.mapping.check_action(held16))
        pose=self.arm.ik.configuration.get_transform_frame_to_world(self.arm.ik.frame)
        result=np.eye(4);result[:3,:3]=pose.rotation;result[:3,3]=pose.translation
        return result.tolist()

    def solve_path(self,*,goals,gripper,full22,held16,scene=None):
        from f1_harness.geometry import transform
        if scene is not None and scene.get('joint_space_search') is True:
            from .harness_joint_plan import solve_joint_path
            return solve_joint_path(self,goals=goals,gripper=gripper,full22=full22,held16=held16,scene=scene)
        deadline=planning_deadline(scene,self.max_wall_s,'bounded_IK_wall_time_exceeded')
        full=np.asarray(full22,float);held=self.mapping.check_action(held16)
        self.arm.begin('right',full,held)
        self.gate.check_preventive(held,full)
        ik=self.arm.ik;q=ik.takeover_q.copy();targets=[];dt=min(.05,ik.safety.max_dt_s)
        initial=ik.configuration.get_transform_frame_to_world(ik.frame)
        previous_pose=np.eye(4);previous_pose[:3,:3]=initial.rotation;previous_pose[:3,3]=initial.translation
        initial_pose=previous_pose.copy()
        redundancy=(scene or {}).get('redundancy_preference')
        if redundancy not in (None,'wrist_center_nullspace'):
            raise ValueError('invalid_redundancy_preference')
        if redundancy is not None:
            # Planning-only soft objective; do not modify model or coupled-wrist
            # limits. F1ArmTarget.begin creates a fresh IK per solve_path.
            ik.posture_task.cost=.1
        posture=None
        posture_path=(scene or {}).get('posture_references_deg')
        if posture_path is not None:
            posture_path=np.asarray(posture_path,float)
            if posture_path.shape!=(len(goals),7) or not np.isfinite(posture_path).all():
                raise ValueError('invalid_demonstrated_posture_path')
            if redundancy is not None:raise ValueError('conflicting_posture_preferences')
            posture_path=np.deg2rad(posture_path)*self.mapping.signs[7:14]
            if np.any(posture_path<ik.model.lowerPositionLimit) or np.any(posture_path>ik.model.upperPositionLimit):
                raise ValueError('success_posture_prior_outside_joint_limits')
            ik.posture_task.cost=.1
        if scene is not None and scene.get('posture_reference_deg') is not None:
            reference=np.asarray(scene['posture_reference_deg'],float)
            if reference.shape!=(7,) or not np.all(np.isfinite(reference)):
                raise ValueError('invalid_success_posture_prior')
            posture=np.deg2rad(reference)*self.mapping.signs[7:14]
            if np.any(posture<ik.model.lowerPositionLimit) or np.any(posture>ik.model.upperPositionLimit):
                raise ValueError('success_posture_prior_outside_joint_limits')
        scene_clear=scene_segment_checker(scene,initial_pose,self.arm.pin if scene and 'maximum_approach_translation_m' in scene else None)
        scene_clear(initial_pose,initial_pose)
        pending_states=[tuple(held)]
        pending_poses=[initial_pose]
        output_segments=[]
        def interpolation_pose(state):
            if time.monotonic()>=deadline:
                raise ValueError('bounded_IK_wall_time_exceeded')
            self.mapping.check_action(state)
            self.gate.check_preventive(state,full)
            ik.anchor(np.deg2rad(np.asarray(state)[7:14])*self.mapping.signs[7:14])
            actual=ik.configuration.get_transform_frame_to_world(ik.frame)
            pose=np.eye(4);pose[:3,:3]=actual.rotation;pose[:3,3]=actual.translation
            return pose
        def finish_dense_segment():
            nonlocal pending_states,pending_poses
            if scene is None:
                targets.append(tuple(held))
                return
            # Every dense state has already passed native protection and the
            # scene sweep. An unfinished route cannot supply output targets.
            output_segments.append((pending_states,pending_poses))
            # Each segment must emit at least one target, plus the final state.
            if len(output_segments)>=100:
                raise ValueError('path_exceeds_bounded_mailbox')
            pending_states=[tuple(held)]
            pending_poses=[previous_pose]
        self.last_solve_diagnostics=[]
        for goal_index,raw in enumerate(goals):
            goal=transform(raw);reached=False;stalled=0
            progress=CartesianProgress(self.position_tolerance,self.rotation_tolerance)
            termination=None
            if posture_path is not None:posture=posture_path[goal_index]
            ik.anchor(q);origin=ik.configuration.get_transform_frame_to_world(ik.frame).copy()
            translation=goal[:3,3]-origin.translation
            rotation=self.arm.pin.log3(goal[:3,:3]@origin.rotation.T)
            subdivisions=max(1,int(np.ceil(max(np.linalg.norm(translation)/(ik.safety.max_linear_speed_m_s*dt),
                                               np.linalg.norm(rotation)/(ik.safety.max_angular_speed_rad_s*dt)))))
            waypoint_index=1;solved_steps=0
            for index in range(self.max_iterations):
                if time.monotonic()>=deadline:
                    raise ValueError('bounded_IK_wall_time_exceeded')
                ik.anchor(q)
                current=ik.configuration.get_transform_frame_to_world(ik.frame)
                fraction=waypoint_index/subdivisions
                waypoint_position=origin.translation+translation*fraction
                waypoint_rotation=self.arm.pin.exp3(rotation*fraction)@origin.rotation
                linear=waypoint_position-current.translation
                angular=self.arm.pin.log3(waypoint_rotation@current.rotation.T)
                if np.linalg.norm(linear)<=self.position_tolerance and np.linalg.norm(angular)<=self.rotation_tolerance:
                    if waypoint_index==subdivisions:
                        reached=True;break
                    waypoint_index+=1
                    continue
                if progress.update(waypoint_index,np.linalg.norm(linear),np.linalg.norm(angular)):
                    termination='cartesian_no_progress';break
                def velocity(error,limit):
                    size=np.linalg.norm(error)
                    return error/max(size,1e-12)*min(limit,size/dt)
                ik.arm_limits.set_publication(q,self.arm.max_otg_ik_lag_rad)
                ik.advance_target(velocity(linear,ik.safety.max_linear_speed_m_s),
                                  velocity(angular,ik.safety.max_angular_speed_rad_s),dt)
                # Cartesian planning may need redundancy motion before a joint
                # saturates. The existing small posture cost biases toward the
                # reviewed successful posture when configured, otherwise joint
                # midranges. It does not change the measured start or QP limits.
                if scene is not None and scene.get('support_contact_tolerance_m') is not None:
                    # During support departure a redundant shoulder/elbow move
                    # can produce second-order downward FK drift. Track the
                    # lift first; use the demonstrated redundancy after lift.
                    target=q
                elif posture_path is not None:
                    target=demonstrated_posture(ik.frame_task.compute_jacobian(ik.configuration),q,posture)
                elif redundancy is not None:
                    target=wrist_center_posture(ik.frame_task.compute_jacobian(ik.configuration),q)
                else:
                    target=posture if posture is not None else (ik.model.lowerPositionLimit+ik.model.upperPositionLimit)/2
                ik.posture_task.set_target(target)
                result=ik.solve(q,dt)
                solved_steps+=1
                stalled=stalled+1 if np.max(np.abs(result.q-q))<1e-9 else 0
                q=result.q.copy()
                candidate=held.copy();candidate[7:14]=np.rad2deg(q)*self.mapping.signs[7:14]
                candidate=self.mapping.check_action(candidate)
                self.gate.check_preventive(candidate,full)
                if scene is not None:
                    ik.anchor(q)
                    actual=ik.configuration.get_transform_frame_to_world(ik.frame)
                    actual_pose=np.eye(4);actual_pose[:3,:3]=actual.rotation;actual_pose[:3,3]=actual.translation
                    scene_clear(previous_pose,actual_pose)
                    previous_pose=actual_pose
                    pending_states.append(tuple(candidate))
                    pending_poses.append(actual_pose)
                held=candidate
                if solved_steps%20==1:finish_dense_segment()
                if stalled>=25:break
            summary=dict(goal_index=goal_index,iterations=index+1,position_error_m=float(np.linalg.norm(linear)),
                rotation_error_rad=float(np.linalg.norm(angular)),goal=goal.tolist(),
                final_arm_deg=held[7:14].tolist(),limits=ik.limit_diagnostics,stalled_ticks=stalled,
                cartesian_waypoint_index=waypoint_index,cartesian_waypoint_count=subdivisions,solves=solved_steps,
                redundancy_preference=redundancy,termination=termination,
                cartesian_stalled_ticks=progress.stalled)
            self.last_solve_diagnostics.append(summary)
            if not reached:
                import json
                raise ValueError('bounded_IK_did_not_reach_goal:'+json.dumps(summary,separators=(',',':')))
            finish_dense_segment()
        # Only a reachable dense route needs output-shortcut validation. Each
        # retained interval still uses the original joint subdivisions, FK and
        # swept geometry, including the dense fallback if a shortcut is blocked.
        try:
            for states,poses in output_segments:
                if len(poses)==1:
                    targets.append(states[-1])
                    continue
                def check_shortcut(start,end):
                    check_joint_interpolation(states[start],states[end],interpolation_pose,scene_clear)
                targets.extend(states[i] for i in sweep_preserving_indices(poses,scene_clear,check_shortcut))
        finally:
            if output_segments:
                ik.anchor(q)
        if gripper is not None:
            held=held.copy();held[15]=float(gripper);self.mapping.check_action(held)
        targets.append(tuple(held))
        if len(targets)>100:raise ValueError('path_exceeds_bounded_mailbox')
        if time.monotonic()>=deadline:raise ValueError('bounded_IK_wall_time_exceeded')
        return [list(t) for t in targets]
