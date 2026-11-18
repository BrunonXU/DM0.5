"""Registered ready pose and measured fixture for the post-placement return skill."""
from dataclasses import dataclass
import numpy as np
from .contracts import HarnessError
from .geometry import transform
from .return_route import propose_return, validate_return_rotation_fraction


def validate_registration(fixture):
    cfg=fixture.get('return_route')
    if not isinstance(cfg,dict) or cfg.get('schema')!='f1-empty-tool-return-v1':
        raise HarnessError('registered_return_route_required')
    home=np.asarray(cfg.get('home_right_deg'),float)
    right=np.asarray(cfg.get('right_axis_ik_world'),float)
    if (home.shape!=(7,) or not np.isfinite(home).all() or right.shape!=(3,)
            or not np.isfinite(right).all() or not np.isclose(np.linalg.norm(right),1.)
            or not cfg.get('source') or not cfg.get('source_sha256')):
        raise HarnessError('registered_return_home_and_axis_required')
    if fixture.get('fixture_reference_hole') is None or not fixture.get('tool_local_boxes'):
        raise HarnessError('registered_return_fixture_and_tool_required')
    if type(cfg.get('continuous_transit',False)) is not bool or (
            cfg.get('continuous_transit') and fixture.get('execution_mode')!='timed_reference'):
        raise HarnessError('continuous_return_requires_timed_reference')
    validate_return_rotation_fraction(cfg.get('rotation_fraction',0.))
    return cfg


@dataclass(frozen=True)
class ReturnGeometry:
    base_eef: np.ndarray
    base_hole: np.ndarray
    home: np.ndarray
    home_right_deg: tuple
    right_axis: tuple
    obstacles: tuple
    tool_local_boxes: tuple
    eef_tip_reference: np.ndarray
    attached_radius_m: float
    planning_margin_m: float
    tool_padding_policy: str
    goal: str = 'placed'
    admission_profile: str = 'development_coarse'
    uncertainty_m: float | None = None
    object_held: bool = False
    target_object_id: str | None = None
    object_local_box: None = None
    eef_object: None = None
    continuous_transit: bool = False
    rotation_fraction: float = 0.

    def proposal(self,variant=0):
        from .skills import tool_margin
        return propose_return(current=self.base_eef,home=self.home,hole=self.base_hole,
            obstacles=self.obstacles,tool_boxes=self.tool_local_boxes,tip=self.eef_tip_reference,
            radius_m=self.attached_radius_m,right_axis=self.right_axis,
            clearance_m=self.planning_margin_m,tool_margin_m=tool_margin(self),variant=variant,
            continuous_transit=self.continuous_transit,rotation_fraction=self.rotation_fraction)


def build_return_scene(service,obs,full,held,*,base_eef=None):
    fixture,task=service.fixture,service.task
    cfg=validate_registration(fixture)
    if (task.admission_profile!='development_coarse' or 'withdraw_reset' not in task.allowed_skills
            or obs.geometry_scope!='coarse_fixture_return'):
        raise HarnessError('fixture_return_scope_required')
    hole=next((o for o in obs.objects if o.object_id==task.hole_id),None)
    if (hole is None or hole.pose is None or hole.shape!=task.shape
            or hole.frame_id!=fixture['native_frame'] or not hole.evidence_ids):
        raise HarnessError('current_return_fixture_required')
    from .gripper_state import observed_open
    opening=[0.,fixture['release_opening']+fixture['gripper_tolerance']]
    if (obs.state16 is None or obs.state_valid_mask is None or not obs.state_valid_mask[15]
            or not observed_open(obs.state16[15],opening) or not observed_open(held[15],opening)):
        raise HarnessError('return_gripper_not_open')
    if base_eef is None:base_eef=service.numerics.call('forward_pose',full22=full,held16=held)
    ready=list(held);ready[7:14]=cfg['home_right_deg']
    home=service.numerics.call('forward_pose',full22=full,held16=ready)
    world_hole=service.frames.world_pose(hole.pose)
    moved=world_hole@np.linalg.inv(service.frames.world_pose(fixture['fixture_reference_hole']))
    obstacles=service.frames.world_obstacles(fixture['obstacles'])
    obstacles=[dict(o,pose=(moved@transform(o['pose'])).tolist()) if o.get('role')=='bucket' else o
               for o in obstacles if not (o.get('role')=='target_object' and o.get('object_id')==task.object_id)]
    return ReturnGeometry(base_eef=transform(base_eef),base_hole=world_hole,home=transform(home),
        home_right_deg=tuple(cfg['home_right_deg']),right_axis=tuple(cfg['right_axis_ik_world']),
        obstacles=tuple(obstacles),tool_local_boxes=tuple(fixture['tool_local_boxes']),
        eef_tip_reference=transform(fixture['eef_tip_reference']),attached_radius_m=fixture['attached_radius_m'],
        planning_margin_m=fixture['planning_margin_m'],tool_padding_policy=fixture.get('tool_padding_policy','required'),
        continuous_transit=cfg.get('continuous_transit',False),rotation_fraction=cfg.get('rotation_fraction',0.))
