"""Versioned contracts; measurements, execution and verdicts stay distinct."""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer


def digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        # Validated content references cover the immutable sensor payloads.
        # Revalidation in the control tick hashes the small scene identity.
        exclude={field for field in ('local_collision_evidence','measured_gripper')
                 if getattr(value,field+'_ref',None) is not None}
        value = value.model_dump(mode="json",exclude=exclude)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Mode(StrEnum):
    FAKE = "fake"
    REPLAY = "replay"
    READ_ONLY = "read_only"
    SHADOW = "shadow"
    HARDWARE = "hardware"


class Frame(Contract):
    camera: str
    stream: Literal["rgb", "depth"]
    frame_id: str
    sequence: int = Field(ge=0)
    source_ns: int | None = Field(default=None, ge=0)
    received_ns: int = Field(ge=0)
    clock_domain: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    encoding: str
    row_step_bytes: int | None = Field(default=None, gt=0)
    is_bigendian: bool = False
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    depth_scale_m: float | None = Field(default=None, gt=0, le=1)
    camera_info_sha256: str | None = None
    source_epoch: int = Field(default=0, ge=0)
    publisher_id: str | None = None


class Pairing(Contract):
    camera: str
    quality: Literal["source_metadata_matched", "source_time_bounded", "receipt_only", "invalid"]
    rgb_sequence: int
    depth_sequence: int
    skew_ns: int = Field(ge=0)
    reason: str = ""


class SceneObject(Contract):
    object_id: str
    shape: Literal["square", "circle", "triangle", "unknown"]
    pose: tuple[float, ...] | None = None
    eef_object_pose: tuple[float, ...] | None = None
    frame_id: str = "unregistered"
    uncertainty_m: float | None = Field(default=None, ge=0)
    planning_margin_m: float | None = Field(default=None, gt=0)
    visible: bool = True
    held: bool | None = None
    inserted: bool | None = None
    inserted_hole_id: str | None = None
    stationary: bool | None = None
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def matrix_shape(self):
        if self.pose is not None and len(self.pose) != 16:
            raise ValueError("pose must be a row-major 4x4 transform")
        if self.eef_object_pose is not None and len(self.eef_object_pose) != 16:
            raise ValueError("in-hand pose must be a row-major 4x4 transform")
        return self


class FixturePose(Contract):
    """Measured rigid fixture reference; never evidence of a visible aperture."""
    fixture_id: str
    pose: tuple[float, ...]
    reference_pose: tuple[float, ...]
    frame_id: str
    evidence_ids: tuple[str, ...]
    source: Literal['rgbd_anchor_registration','retained_static_fixture'] = 'rgbd_anchor_registration'
    measured_at_ns: int | None = None

    @model_validator(mode='after')
    def transforms(self):
        if len(self.pose)!=16 or len(self.reference_pose)!=16 or not self.evidence_ids:
            raise ValueError('fixture_pose_and_source_evidence_required')
        if self.source=='retained_static_fixture' and (self.measured_at_ns is None or self.measured_at_ns<0):
            raise ValueError('retained_fixture_requires_original_measurement_time')
        return self


class FreeSpaceView(Contract):
    """Conservative current depth tiles; zero depth is unobserved, never free."""
    camera: Literal['head', 'right']
    camera_pose_world: tuple[float, ...]
    intrinsics: tuple[float, ...]
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    tile_size_px: int = Field(gt=0)
    grid_width: int = Field(gt=0)
    grid_height: int = Field(gt=0)
    min_depth_m: tuple[float, ...]
    near_depth_m: float = Field(ge=0)
    source_rgb_sha256: str
    source_depth_sha256: str
    fine_depth_zlib_base64: str | None = None
    fine_depth_sha256: str | None = None

    @model_validator(mode='after')
    def dimensions(self):
        if (len(self.camera_pose_world)!=16 or len(self.intrinsics)!=9
                or len(self.min_depth_m)!=self.grid_width*self.grid_height
                or self.grid_width!=(self.image_width+self.tile_size_px-1)//self.tile_size_px
                or self.grid_height!=(self.image_height+self.tile_size_px-1)//self.tile_size_px
                or any(value<0 for value in self.min_depth_m)):
            raise ValueError('invalid_local_free_space_grid')
        if (self.fine_depth_zlib_base64 is None)!=(self.fine_depth_sha256 is None):
            raise ValueError('local_fine_depth_identity_required')
        return self


class SurfaceSupportingPlane(Contract):
    """A measured outward face of the registered convex solid box."""
    centre_world: tuple[float,float,float]
    normal_world: tuple[float,float,float]
    selected_normal_world: tuple[float,float,float]
    normal_difference_rad: float = Field(ge=0)
    normal_tolerance_rad: float = Field(gt=0)
    camera_plane_signed_distance_m: float = Field(gt=0)
    all_target_front_side_max_m: float
    residual_p95_m: float = Field(ge=0)

    @model_validator(mode='after')
    def measured_face(self):
        if (self.normal_difference_rad>self.normal_tolerance_rad
                or abs(sum(x*x for x in self.normal_world)-1)>1e-6
                or abs(sum(x*x for x in self.selected_normal_world)-1)>1e-6):
            raise ValueError('local_surface_supporting_plane_invalid')
        return self


class SurfaceAnchorEvidence(Contract):
    """Current consensus points on the identified target, without a pose fit."""
    source: Literal['current_mask_planar_inliers'] = 'current_mask_planar_inliers'
    bounds_min: tuple[float,float,float]
    bounds_max: tuple[float,float,float]
    inlier_points: int = Field(gt=0)
    minimum_points: int = Field(ge=20)
    normal_world: tuple[float,float,float]
    residual_p95_m: float = Field(ge=0)
    distance_threshold_m: float = Field(gt=0)
    competing_plane_excluded: Literal[True]
    extremal_points_world: tuple[tuple[float,float,float], ...] = ()
    supporting_plane: SurfaceSupportingPlane | None = None

    @model_validator(mode='after')
    def observed_support(self):
        if (self.inlier_points<self.minimum_points or self.residual_p95_m>self.distance_threshold_m+1e-9
                or any(a>b for a,b in zip(self.bounds_min,self.bounds_max))
                or abs(sum(x*x for x in self.normal_world)-1)>1e-6):
            raise ValueError('local_surface_anchor_support_invalid')
        if self.extremal_points_world and (len(self.extremal_points_world)!=6
                or any(any(value<low-1e-9 or value>high+1e-9
                       for value,low,high in zip(point,self.bounds_min,self.bounds_max))
                       or abs(point[index//2]-(self.bounds_min if index%2==0 else self.bounds_max)[index//2])>1e-9
                       for index,point in enumerate(self.extremal_points_world))):
            raise ValueError('local_surface_anchor_extremal_points_invalid')
        if self.supporting_plane is not None:
            plane=self.supporting_plane
            if (plane.residual_p95_m>self.distance_threshold_m
                    or any(value<low-1e-9 or value>high+1e-9
                           for value,low,high in zip(plane.centre_world,self.bounds_min,self.bounds_max))):
                raise ValueError('local_surface_supporting_plane_invalid')
        return self


class LocalCollisionEvidence(Contract):
    """An observed target region without an invented object coordinate frame."""
    schema_version: Literal['f1-local-collision-evidence-v1'] = 'f1-local-collision-evidence-v1'
    scope: Literal['registered_single_object_scene'] = 'registered_single_object_scene'
    object_id: str
    frame_id: str
    observation_id: str
    calibration_id: str
    measured_at_ns: int = Field(ge=0)
    state_received_ns: int = Field(ge=0)
    source_frames: tuple[Frame, ...]
    observed_surface_min: tuple[float, float, float]
    observed_surface_max: tuple[float, float, float]
    possible_occupancy_min: tuple[float, float, float]
    possible_occupancy_max: tuple[float, float, float]
    registered_object_diagonal_m: float = Field(gt=0)
    planning_margin_m: float = Field(ge=0)
    depth_points: int = Field(gt=0)
    mask_pixels: int = Field(gt=0)
    free_space_views: tuple[FreeSpaceView, ...]
    surface_anchor: SurfaceAnchorEvidence | None = None

    @model_validator(mode='after')
    def conservative_bounds(self):
        radius=self.registered_object_diagonal_m+self.planning_margin_m
        minimum=[p-radius for p in self.observed_surface_min]
        maximum=[p+radius for p in self.observed_surface_max]
        if self.surface_anchor is not None:
            anchor=self.surface_anchor
            if (anchor.supporting_plane is not None
                    and anchor.supporting_plane.all_target_front_side_max_m>self.planning_margin_m):
                raise ValueError('local_surface_supporting_plane_contradicted')
            if (anchor.inlier_points>self.depth_points
                    or any(a<b-1e-9 for a,b in zip(anchor.bounds_min,self.observed_surface_min))
                    or any(a>b+1e-9 for a,b in zip(anchor.bounds_max,self.observed_surface_max))):
                raise ValueError('local_surface_anchor_outside_observed_points')
            # Every point of one rigid object is within its maximum diameter
            # of EVERY measured anchor. Intersect those bounds; no unseen
            # face orientation or object coordinate frame is invented.
            minimum=[p-radius for p in anchor.bounds_max]
            maximum=[p+radius for p in anchor.bounds_min]
        if (not self.source_frames or not self.free_space_views or self.depth_points>self.mask_pixels
                or any(a>b for a,b in zip(self.observed_surface_min,self.observed_surface_max))
                or any(a>b for a,b in zip(minimum,maximum))
                or any(low>bound+1e-9 for low,bound in zip(self.possible_occupancy_min,minimum))
                or any(high<bound-1e-9 for high,bound in zip(self.possible_occupancy_max,maximum))):
            raise ValueError('invalid_local_target_occupancy_evidence')
        return self


class LocalCollisionEvidenceReference(Contract):
    """Run-local transport identity, never sufficient evidence to authorize motion."""
    schema_version: Literal['f1-local-evidence-reference-v1'] = 'f1-local-evidence-reference-v1'
    store_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    blob_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    byte_count: int = Field(gt=0, le=16*1024*1024)
    observation_id: str
    calibration_id: str
    cameras: tuple[Literal['head','right'], ...] = Field(min_length=1,max_length=2)


class MeasuredGripperSample(Contract):
    """Independent external status, not the command-filled ROS closure field."""
    source_instance: str = Field(min_length=1)
    sequence: int = Field(strict=True, gt=0)
    received_ns: int = Field(strict=True, ge=0)
    kernel_received_realtime_ns: int | None = Field(default=None, strict=True, ge=0)
    queue_delay_ns: int | None = Field(default=None, strict=True)
    time_valid: bool = Field(strict=True)
    frame_format: Literal['classic_can', 'can_fd']
    can_fd_flags: int | None = Field(default=None, strict=True, ge=0, le=255)
    raw_position: int = Field(strict=True, ge=0, le=255)
    closure_percent: float = Field(ge=0, le=100)
    velocity_raw: int = Field(strict=True, ge=0, le=255)
    current_raw: int = Field(strict=True, ge=0, le=255)
    data_hex: str = Field(pattern=r'^[0-9a-f]{16}$')

    @model_validator(mode='after')
    def original_measurement(self):
        if self.time_valid and (self.kernel_received_realtime_ns is None
                or self.queue_delay_ns is None or self.queue_delay_ns < 0):
            raise ValueError('independent_gripper_time_missing')
        if (self.frame_format == 'can_fd') != (self.can_fd_flags is not None):
            raise ValueError('gripper_frame_format_mismatch')
        data = bytes.fromhex(self.data_hex)
        if (tuple(data[2:5]) != (self.raw_position, self.velocity_raw, self.current_raw)
                or abs(self.closure_percent - (255-self.raw_position)*100./255) > 1e-9):
            raise ValueError('gripper_raw_measurement_mismatch')
        return self


class MeasuredGripperFeedback(Contract):
    model_config = ConfigDict(serialize_by_alias=True)
    schema_version: Literal['f1-gripper-feedback-v1'] = Field(alias='schema')
    source: Literal['socketcan_external']
    protocol: Literal['omnipicker_status_v1']
    side: Literal['left', 'right']
    interface: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,15}$')
    response_id: int = Field(strict=True, ge=0, le=0x7ff)
    source_instance: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    clock_domain: Literal['host_monotonic']
    position_unit: Literal['closure_0_open_100_closed']
    position_resolution: float = Field(gt=0)
    running: bool = Field(strict=True)
    error: str | None = None
    received_count: int = Field(strict=True, ge=0)
    rejected_count: int = Field(strict=True, ge=0)
    latest: MeasuredGripperSample | None = None
    samples: tuple[MeasuredGripperSample, ...] = Field(default=(), max_length=1024)

    @model_validator(mode='after')
    def one_source_window(self):
        if abs(self.position_resolution - 100./255) > 1e-9:
            raise ValueError('gripper_resolution_mismatch')
        if any(row.source_instance != self.source_instance for row in self.samples):
            raise ValueError('gripper_source_epoch_mismatch')
        if any(a.sequence >= b.sequence or a.received_ns > b.received_ns
               for a, b in zip(self.samples, self.samples[1:])):
            raise ValueError('gripper_measurement_order_invalid')
        if self.latest != (self.samples[-1] if self.samples else None):
            raise ValueError('gripper_latest_not_in_window')
        if self.samples and self.received_count < self.samples[-1].sequence:
            raise ValueError('gripper_received_count_invalid')
        return self


class MeasuredGripperReference(Contract):
    """Bounded reference to the complete, unchanged independent CAN window."""
    schema_version: Literal['f1-measured-gripper-reference-v1'] = 'f1-measured-gripper-reference-v1'
    store_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    blob_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    byte_count: int = Field(strict=True, gt=0, le=16*1024*1024)
    observation_id: str
    acquisition_instance: str
    host_boot_id: str | None
    received_ns: int = Field(strict=True, ge=0)
    state_received_ns: int | None = Field(strict=True, ge=0)
    frames_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_instance: str = Field(min_length=1)
    sensor_boot_id: str = Field(min_length=1)
    first_sequence: int | None = Field(strict=True, gt=0)
    last_sequence: int | None = Field(strict=True, gt=0)

    @model_validator(mode='after')
    def complete_sequence_interval(self):
        if ((self.first_sequence is None)!=(self.last_sequence is None)
                or self.first_sequence is not None and self.last_sequence < self.first_sequence):
            raise ValueError('measured_gripper_reference_sequence_invalid')
        return self


class Observation(Contract):
    schema_version: Literal["f1-harness-observation-v1"] = "f1-harness-observation-v1"
    observation_id: str
    acquisition_instance: str
    received_ns: int = Field(ge=0)
    clock_domain: str
    frames: tuple[Frame, ...] = ()
    host_boot_id: str | None = None
    pairings: tuple[Pairing, ...] = ()
    state16: tuple[float, ...] | None = None
    state_valid_mask: tuple[bool, ...] | None = None
    state_received_ns: int | None = None
    measured_gripper: MeasuredGripperFeedback | None = None
    measured_gripper_ref: MeasuredGripperReference | None = None
    owner_observation_id: str | None = None
    calibration_id: str | None = None
    scene_version: str = "unavailable"
    objects: tuple[SceneObject, ...] = ()
    fixture: FixturePose | None = None
    local_collision_evidence: LocalCollisionEvidence | None = None
    local_collision_evidence_ref: LocalCollisionEvidenceReference | None = None
    stationary: bool = False
    synthetic: bool = False
    geometry_scope: Literal["precision", "coarse_static_approach", "coarse_static_task", "coarse_object_verification", "coarse_fixture_return", "coarse_local_environment"] = "precision"
    geometry_camera: Literal["head", "right"] | None = None
    faults: tuple[str, ...] = ()
    quality_warnings: tuple[str, ...] = ()

    @model_serializer(mode='wrap')
    def preserve_observation_identity(self,handler):
        # These optional fields did not exist in older signed trials.
        # Omit only their absent defaults; every older null and serializer option
        # stays intact, including nested Observation / model_dump_json calls.
        value=handler(self)
        if self.local_collision_evidence_ref is None:
            value.pop('local_collision_evidence_ref',None)
        if self.measured_gripper is None:
            value.pop('measured_gripper',None)
        if self.measured_gripper_ref is None:
            value.pop('measured_gripper_ref',None)
        return value

    @model_validator(mode="after")
    def state_shape(self):
        if self.state16 is not None and len(self.state16) != 16:
            raise ValueError("state16 must have 16 physical values")
        if self.state_valid_mask is not None and len(self.state_valid_mask) != 16:
            raise ValueError("state mask must have 16 values")
        return self


PLACEMENT_SKILLS = ("approach_grasp", "grasp", "verify_grasp", "inspect_in_hand", "transfer_above_hole",
          "align", "insert", "release", "verify_placed", "retreat", "reobserve", "regrasp")


SKILLS = PLACEMENT_SKILLS + ("withdraw_reset", "move_tcp", "recover_grasp", "release_supported")
# One execution classification for owner lifecycle, recovery and feedback.
# Verification may update measured facts but never owns a trajectory.
VERIFICATION_SKILLS = frozenset(("verify_grasp", "inspect_in_hand", "verify_placed", "reobserve"))
MOTION_SKILLS = frozenset(SKILLS) - VERIFICATION_SKILLS


class Task(Contract):
    task_id: str
    object_id: str
    hole_id: str
    shape: Literal["square", "circle", "triangle"] = "square"
    allowed_skills: tuple[str, ...] = PLACEMENT_SKILLS
    local_corrections: int = Field(default=2, ge=0, le=4)
    regrasp_limit: int = Field(default=1, ge=0, le=3)
    calibration_id: str
    goal: Literal["placed", "approach_grasp"] = "placed"
    admission_profile: Literal["precision", "development_coarse"] = "precision"
    operator_instruction: str = Field(default='',max_length=1000)

    @model_validator(mode="after")
    def known_skills(self):
        if not self.allowed_skills or set(self.allowed_skills) - set(SKILLS):
            raise ValueError("only registered skills may be authorized")
        if self.goal == "approach_grasp" and (set(self.allowed_skills) != {"approach_grasp", "reobserve"}
                or self.local_corrections or self.regrasp_limit):
            raise ValueError("approach task requires exact noncontact skill scope and zero recovery")
        return self


class MotionParameters(Contract):
    """Task-relative choices; never joint commands, calibration or success claims."""
    pitch_offset_deg: float | None = Field(default=None, ge=-45, le=45, strict=True)
    variant: int | None = Field(default=None, ge=0, le=3, strict=True)
    grasp_tcp_pose: tuple[float, ...] | None = None
    approach_direction_object: tuple[float, float, float] | None = None
    approach_distance_m: float | None = Field(default=None, gt=0, strict=True)
    target_tcp_pose: tuple[float, ...] | None = None
    via_tcp_poses: tuple[tuple[float, ...], ...] = Field(default=(), max_length=12)
    target_frame: Literal['world', 'object', 'hole'] = 'world'
    support_contact: bool = Field(default=False, strict=True)

    @model_validator(mode='after')
    def geometric_targets(self):
        from .geometry import transform
        import math
        for pose in (self.grasp_tcp_pose, self.target_tcp_pose, *self.via_tcp_poses):
            if pose is not None:
                if len(pose)!=16:raise ValueError('row_major_SE3_required')
                transform(pose)
        if self.approach_direction_object is not None:
            if math.sqrt(sum(x*x for x in self.approach_direction_object))<1e-8:
                raise ValueError('nonzero_approach_direction_required')
        custom=(self.grasp_tcp_pose is not None or self.approach_direction_object is not None or self.approach_distance_m is not None)
        if custom and (self.pitch_offset_deg is not None or self.variant is not None):
            raise ValueError('explicit_grasp_pose_does_not_use_legacy_angles')
        return self

    def for_skill(self, skill):
        if self.support_contact and skill!='move_tcp':
            raise ValueError('support_contact_requires_move_tcp')
        if (self.grasp_tcp_pose is not None or self.approach_direction_object is not None
                or self.approach_distance_m is not None) and skill not in ('approach_grasp','regrasp'):
            raise ValueError('grasp_geometry_only_before_grasp')
        if self.target_tcp_pose is not None or self.via_tcp_poses or self.target_frame!='world':
            if skill!='move_tcp':raise ValueError('cartesian_targets_require_move_tcp')
        if skill=='move_tcp' and self.target_tcp_pose is None:
            raise ValueError('move_tcp_target_required')
        if self.pitch_offset_deg is not None and skill not in ('approach_grasp', 'regrasp'):
            raise ValueError('pitch_only_before_grasp')
        if self.variant is not None and skill not in ('approach_grasp', 'regrasp', 'transfer_above_hole', 'align', 'withdraw_reset'):
            raise ValueError('variant_not_supported_for_skill')
        return self

    def supplied(self):
        """Only explicit choices travel across older tool contracts."""
        return self.model_dump(exclude_none=True, exclude_defaults=True)


class SkillIntent(Contract):
    schema_version: Literal["f1-harness-skill-v1"] = "f1-harness-skill-v1"
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str
    owner_instance_id: str
    task_id: str
    task_hash: str
    epoch: int = Field(ge=0)
    observation_id: str
    scene_version: str
    calibration_id: str
    skill: str
    budget_ms: int = Field(default=10000, gt=0, le=300000)
    candidate_start: int = Field(default=0, ge=0, le=3)
    excluded_candidates: tuple[int, ...] = Field(default=(), max_length=4)
    parameters: MotionParameters = Field(default_factory=MotionParameters)

    @model_validator(mode="after")
    def registered_candidate_exclusions(self):
        if not ((self.resume_request_id or self.verification_request_id) and not self.parameters.supplied()):
            self.parameters.for_skill(self.skill)
        if len(set(self.excluded_candidates))!=len(self.excluded_candidates) or any(v not in range(4) for v in self.excluded_candidates):
            raise ValueError('registered_candidate_exclusions_required')
        return self
    resume_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    verification_request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='after')
    def distinct_continuation(self):
        if self.resume_request_id and self.verification_request_id:
            raise ValueError('motion_and_verification_continuations_are_distinct')
        return self

class SkillResult(Contract):
    request_id: str
    lifecycle: Literal["accepted", "running", "paused", "completed", "cancelled", "preempted", "rejected"]
    outcome: Literal["success", "failure", "pending_review", "interrupted"] = "pending_review"
    reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    assisted: bool = False
    reward_known: bool = False
    training_ready: Literal[False] = False
    cancel_stage: Literal["none", "cancel_requested", "owner_hold_committed", "measured_stationary"] = "none"
    synthetic: bool = False
    no_motion: bool = False
    retryable: bool = False
    rejected_candidate_variant: int | None = Field(default=None, ge=0, le=3)
    resume_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    verification_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Bounded owner-produced explanation; data for planning, never authority
    # to retry a motion or override the request lifecycle.
    planning_diagnostic: dict | None = None

    @model_validator(mode="after")
    def retry_requires_confirmed_unpublished_failure(self):
        if self.verification_request_id is not None and (
                self.lifecycle!='completed' or self.outcome!='pending_review'
                or not self.reason.startswith('post_motion_verification_unavailable:')
                or self.resume_request_id or self.retryable):
            raise ValueError('verification_continuation_requires_missing_evidence')
        if self.resume_request_id is not None and (self.lifecycle!='completed' or self.outcome!='failure'
                or self.reason!='motion_stalled' or self.no_motion or self.retryable):
            raise ValueError('resume_requires_known_stalled_motion')
        if self.retryable and (not self.no_motion or self.lifecycle!='completed' or self.outcome!='failure'):
            raise ValueError('retry_requires_confirmed_unpublished_failure')
        return self


class HarnessError(RuntimeError):
    """Stable, non-secret reason code usable in tool and transport errors."""
