"""Small adapters to pinned RPent and PyRUA; no hardware publisher lives here."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
import uuid

from pyrualean._robot import RobotBase
from pyrualean.arms import ArmToolkit
from pyrualean.robots import RobotAdapter
from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory.manager import MemoryManager
from rpent.tools.toolkit import Toolkit, readonly

from .contracts import HarnessError, SKILLS, SkillIntent, MotionParameters, digest


def _planning_feedback(value):
    """Small diagnostic projection for model feedback, never scene/image payloads."""
    if not isinstance(value, dict):
        return None
    metric_keys = frozenset({
        'stage_index', 'goal_index', 'iterations', 'position_error_m', 'rotation_error_rad',
        'termination','cartesian_stalled_ticks',
        'cartesian_waypoint_index', 'cartesian_waypoint_count', 'min_constraint_margin_rad',
        'route_family', 'segment_index', 'obstacle_index', 'obstacle_role', 'obstacle_name',
        'collision_body', 'variant', 'clearance_m', 'skill', 'held', 'inserted', 'visible',
        'position_tolerance_m', 'rotation_tolerance_rad', 'object_id', 'observation_id',
        'collision_pair', 'active_planes', 'arm_collision_indices',
        'grasp_strategy', 'grasp_pitch_offset_deg',
        'continuation_skill','continuation_code',
        'reused_failure','previous_request_id','recovery_hint','reason',
        'start_pose_clear','end_pose_clear','collision_location',
        'segment_start_tcp_pose','segment_end_tcp_pose',
        'search_phase','ik_seed_count','ik_pose_fit_count','native_endpoint_checks','native_valid_goal_count',
        'ompl_started','best_fit_native_checked','best_fit_native_valid','endpoint_rejection',
        'best_fit_executable','search_exhaustive','position_error_reference',
        'target_tcp_pose','best_fit_tcp_pose','target_minus_fit_tcp_m','frame',
        'path_checked','grasp_checked','motion_authorized',
        'cell_min','cell_max','possible_target_overlap','cells_tested',
        'refinement_budget_exhausted','evidence_observation_id','unknown_space_is_free','clear',
    })

    def scalar(item):
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return item[:160]
        if isinstance(item, (int, float)):
            try:
                return item if math.isfinite(item) else None
            except OverflowError:
                return None
        return None

    def entry(item):
        result = {}
        for key in ('code', 'error_type'):
            if isinstance(item.get(key), str):
                result[key] = item[key][:96]
        if type(item.get('variant')) is int and 0 <= item['variant'] <= 3:
            result['variant'] = item['variant']
        metrics = item.get('metrics')
        if isinstance(metrics, dict):
            result['metrics'] = {
                key: ([scalar(v) for v in content[:16 if key in (
                    'segment_start_tcp_pose','segment_end_tcp_pose','target_tcp_pose','best_fit_tcp_pose') else 8]] if isinstance(content, (list, tuple))
                      else scalar(content))
                for key, content in metrics.items() if key in metric_keys
            }
            if isinstance(metrics.get('views'),list):
                views=[]
                for view in metrics['views'][:2]:
                    if not isinstance(view,dict):continue
                    row={key:scalar(view[key]) for key in (
                        'camera','reason','unknown_pixels','unknown_tiles','z_min','z_max','nearest_depth_m','margin_m')
                        if key in view}
                    for key in ('pixel_min','pixel_max'):
                        if isinstance(view.get(key),(list,tuple)):
                            row[key]=[scalar(item) for item in view[key][:2]]
                    views.append(row)
                result['metrics']['views']=views
            hint=metrics.get('endpoint_clearance_hint')
            if isinstance(hint,dict):
                projected={key:scalar(hint.get(key)) for key in
                    ('frame','distance_m','scope','path_checked','grasp_checked','motion_authorized')}
                vector=hint.get('translation_m')
                if isinstance(vector,(list,tuple)) and len(vector)==3:
                    projected['translation_m']=[scalar(v) for v in vector]
                    result['metrics']['endpoint_clearance_hint']=projected
        return result

    result = entry(value)
    if isinstance(value.get('last_candidate_failure'),dict):
        result['last_candidate_failure']=entry(value['last_candidate_failure'])
    missing=(value.get('metrics') or {}).get('missing_entities')
    if isinstance(missing,list):
        result.setdefault('metrics',{})['missing_entities']=[{key:scalar(row.get(key))
            for key in ('role','object_id','reason','recovery')} for row in missing[:4] if isinstance(row,dict)]
    if isinstance(value.get('candidates'), list):
        candidates=value['candidates']
        sample=candidates if len(candidates)<=4 else candidates[:2]+candidates[-2:]
        result['candidate_count']=value.get('candidate_count',len(candidates))
        result['detail_truncated']=len(candidates)>4
        result['candidates'] = [entry(item) for item in sample
                                if isinstance(item, dict)]
    if value.get('detail_truncated'):
        result['detail_truncated'] = True
    from .planning_diagnostics import bound_detail
    return bound_detail(result)


@dataclass(frozen=True)
class F1State:
    observation_id: str
    phase: str
    objects: tuple[dict, ...]
    authorized: bool
    task: str = ""
    terminated: bool = False
    truncated: bool = False
    geometry_scope: str | None = None
    geometry: dict | None = None
    quality_warnings: tuple[str, ...] = ()


class F1Robot(RobotBase):
    NAME = "f1"
    CAMERAS = ("head", "right", "left")
    ABSENT_MEMBERS = frozenset({"world_map"})
    STATE_TOOL = "observe"
    STATEFUL_TOOLS = frozenset({"execute_skill", "locate_target", "locate_entities"})
    # ArmToolkit sends ledger summaries back even when a generated cell assigns
    # the result instead of printing it. RPC ok=True is not physical success.
    SUMMARY_KEYS = RobotBase.SUMMARY_KEYS + (
        'request_id', 'lifecycle', 'outcome', 'reason', 'no_motion', 'retryable',
        'cancel_stage', 'rejected_candidate_variant', 'resume_request_id',
        'verification_request_id', 'planning_diagnostic', 'observation_retry_reason', 'continuation_submitted',
        'planned', 'current_scope', 'executable', 'stages', 'candidate_variant', 'placement_advisory',
        'accepted', 'search_only', 'motion_evidence',
        'missing_entities','geometry_status',
        'fixture_transit_available','aperture_required_for',
        'recovery_context',
    )
    IMAGE_ARTIFACTS = {(camera, "high"): camera + ".png" for camera in CAMERAS}

    @property
    def task(self) -> str:
        """Read the prepared instruction without waiting for images or geometry."""
        return self._readonly('task_context', {})['instruction']

    def _summary(self, tool, payload):
        summary = super()._summary(tool, payload)
        if tool=='locate_entities' and payload.get('accepted') is False:
            summary.update({key:payload[key] for key in ('camera','unsupported_entities','search_entities','next')
                            if key in payload})
        if tool == 'aperture_observation':
            return dict(summary, **{k: payload[k] for k in ('available', 'frame', 'camera', 'candidates',
                'source_ns', 'source_blob_sha256', 'motion_evidence', 'reason') if k in payload})
        if isinstance(payload.get('left_aperture_observation'), dict):
            summary['left_aperture_observation'] = payload['left_aperture_observation']
        if isinstance(payload.get('condition'),dict):
            from .conditions import compact_observation_condition
            summary['condition']=compact_observation_condition(payload['condition'])
        if tool == 'inspect_plan' and isinstance(payload.get('preview'), dict):
            # inspect_plan returns an envelope, unlike preview_skill. Preserve
            # the same selection evidence when the model assigns (not prints)
            # the envelope; otherwise successful previews lose their advice.
            for key in ('stages', 'candidate_variant', 'placement_advisory',
                        'no_motion', 'executable'):
                if key in payload['preview']:
                    summary[key] = payload['preview'][key]
        if 'planning_diagnostic' in summary:
            summary['planning_diagnostic'] = _planning_feedback(summary['planning_diagnostic'])
        if tool=='geometry_context' or (tool=='inspect_plan' and payload.get('planned') is not True
                                        and payload.get('geometry_available') is not False):
            from .planner_geometry import pack_geometry
            summary['geometry']=pack_geometry(payload if tool=='geometry_context' else payload.get('geometry'))
        elif tool=='inspect_plan' and payload.get('geometry_available') is False:
            summary.update({key:payload[key] for key in ('available_skills','cartesian_motion','geometry_available')
                            if key in payload})
        if tool in ('geometry_context','inspect_plan'):
            # A failed trajectory can still carry newly measured geometry.
            # Keep this semantic evidence even when the large geometry payload
            # is omitted for a feasible preview. Never infer it from a prompt,
            # planned=True, or geometry_status alone.
            geometry=payload if tool=='geometry_context' else payload.get('geometry')
            if isinstance(geometry,dict):
                summary['scene_entities']=[{key:entity.get(key) for key in
                    ('object_id','visible','has_pose','held','inserted')}
                    for entity in geometry.get('entities',[]) if isinstance(entity,dict)]
        if tool == 'observe':
            observation = payload.get('observation', payload.get('state', payload)) or {}
            from .planner_geometry import observation_warnings
            summary['quality_warnings']=observation_warnings(observation.get('quality_warnings'))
            summary['scene_entities'] = [dict(object_id=obj.get('object_id'),
                visible=obj.get('visible'), has_pose=obj.get('pose') is not None,
                held=obj.get('held'), inserted=obj.get('inserted'))
                for obj in observation.get('objects', []) if isinstance(obj, dict)]
            from .planner_geometry import has_local_planning_context,pack_geometry
            if has_local_planning_context(observation,payload.get('geometry')):
                summary['geometry_scope']=observation['geometry_scope']
                summary['geometry']=pack_geometry(payload.get('geometry'))
        if tool != 'execute_skill':
            return summary
        # Retain the fact that the owner supplied evidence without expanding
        # immutable evidence IDs into every model turn.
        summary['evidence_recorded'] = bool(payload.get('evidence_ids'))
        bounded = {}
        for key, value in summary.items():
            if key == 'planning_diagnostic':
                bounded[key] = _planning_feedback(value)
            elif key == 'recovery_context' and isinstance(value,dict):
                from .planner_geometry import recovery_geometry
                context={k:value[k][:200] if isinstance(value[k],str) else value[k]
                    for k in ('state','source','error_type','reason','failure_class')
                    if k in value and (value[k] is None or isinstance(value[k],(str,bool)))}
                context['motion_authorized']=False
                geometry=value.get('geometry')
                if isinstance(geometry,dict) and geometry.get('schema')=='f1-recovery-geometry-v1':
                    context['geometry']=recovery_geometry(dict(geometry,schema='f1-geometric-tools-v1'))
                bounded[key]=context
            elif isinstance(value, str):
                bounded[key] = value[:512 if key in ('reason','observation_retry_reason') else 128]
            elif value is None or isinstance(value, (bool, int, float)):
                bounded[key] = value
            elif key == 'missing_entities' and isinstance(value, list):
                bounded[key] = [{k: str(row[k])[:160] for k in ('object_id','role','reason','recovery')
                                 if row.get(k) is not None} for row in value[:4] if isinstance(row,dict)]
        return bounded

    def _state_from_envelope(self, value):
        from .planner_geometry import pack_geometry,observation_warnings,has_local_planning_context
        obs = value.get("state", value.get("observation", value)) or {}
        status = value.get("status", {})
        return F1State(observation_id=obs.get("observation_id", "unavailable"),
                       phase=status.get("phase", "unknown"), objects=tuple(obs.get("objects", ())),
                       authorized=status.get("authorized", False), terminated=value.get("terminated", False),
                       task=value.get('task_instruction', ''),geometry_scope=obs.get('geometry_scope'),
                       quality_warnings=tuple(observation_warnings(obs.get('quality_warnings'))),
                       geometry=(pack_geometry(value.get('geometry'))
                                 if has_local_planning_context(obs,value.get('geometry')) else None))

    def _summary_dict(self):
        # Per-cell display must never initiate perception or consume its wait
        # budget. Only an explicit observe/skill requests fresh geometry.
        state = self._last_state
        return dict(phase=state.phase if state else 'unobserved',
                    observation_id=state.observation_id if state else 'unavailable',
                    authorized=state.authorized if state else None,state_cached=True)

    def aperture_observation(self) -> dict:
        """Read current left-wrist aperture shape candidates in raw image pixels.
        Includes frame identity, corners, and circle ellipse when visible. The
        left arm stays at its current pose. No image is sent, no 3D pose is inferred.
        """
        return self._readonly('aperture_observation', {})

    def locate_object(self, camera: str, box_normalized: list[float]) -> dict:
        """Suggest where to find the prepared block in the last image you viewed.

        First show/image head or right. Box [left,top,right,bottom] is normalized
        to that displayed image, each coordinate 0..1. Updates a segmentation
        search prompt only; never a robot pose or success. Then observe again
        for fresh RGB-D geometry before previewing/submitting any motion.
        """
        return self._stateful('locate_target',dict(camera=camera,box_normalized=box_normalized),lambda value:value)

    def observe(self) -> dict:
        """Get fresh evidence or a structured missing-observation result.

        Incomplete scene geometry can still include a measured target pose and
        local observation context. Inspect the missing entities and available
        skills before choosing a new observation.
        """
        value=self._readonly("observe", {})
        # F1's observation envelope is not RPent's nested result shape. Keep
        # the same measured/unknown state for subsequent state summaries.
        self._absorb(value)
        return value

    def locate_entities(self, camera: str, entities: list[dict]) -> dict:
        """Prompt registered block/aperture/fixture IDs in the last shown image.

        Each entry: object_id, box_normalized=[left,top,right,bottom], optional
        points_normalized=[[x,y],...] and point_labels=[1 foreground,0 background].
        Coordinates use that displayed full image. Batch related fixture IDs
        in one call. Use geometry()['search_entities'][camera] for the site's IDs.
        A wrong-view batch returns accepted=False with the catalog; boxes cannot
        be transferred between views. Accepted hints still require fresh RGB-D.
        """
        return self._stateful('locate_entities',dict(camera=camera,entities=entities),lambda value:value)

    def geometry(self) -> dict:
        """Read current TCP, object/hole frames, nominal grasp and obstacle geometry.

        Poses are row-major SE(3), metres. Native world identity is returned.
        geometry_status is 'complete', 'partial' or 'unavailable'. Check missing_entities
        and individual poses. available_skills lists task skills eligible for preview
        at this geometry scope; motion still requires a successful owner check.
        search_entities maps each registered view to its image-prompt IDs.
        """
        return self._readonly('geometry_context',{})

    def preview(self, name: str, **parameters) -> dict:
        """Run the same geometric/IK planner without movement. A preview never authorizes execution.

        Accepts skill parameters including grasp_tcp_pose (object-relative TCP),
        approach_direction_object (vector pointing FROM grasp TO pregrasp),
        approach_distance_m, or move_tcp target_tcp_pose/via_tcp_poses/target_frame.
        Check planned and current_scope; failure describes a rejected proposal,
        not global unreachability. Execute subsequently with fresh measurements.
        """
        params=MotionParameters.model_validate(parameters).for_skill(name)
        request=dict(name=name,parameters=params.supplied())
        try:
            return self._readonly('preview_skill',request)
        except RuntimeError as exc:
            # Only a rejected read-only preview. Never retry an execute_skill,
            # transport uncertainty, identity change or partially sent motion.
            if not str(exc).endswith(('stale_observation_measurements','scene_expired')):
                raise
            self.observe()
            return self._readonly('preview_skill',request)

    def inspect_plan(self, name: str = 'approach_grasp', **parameters) -> dict:
        """Acquire fresh observation, geometry and a read-only candidate preview together.

        Returned facts are for choosing a program; execution still revalidates.
        This never moves, relaxes checks, or replays a submitted motion.
        """
        params=MotionParameters.model_validate(parameters).for_skill(name)
        return self._readonly('inspect_plan',dict(name=name,parameters=params.supplied()))


    def move_to(self, target_tcp_pose: list[float], *, frame: str='world', via: list | None=None) -> dict:
        """Move TCP through continuous SE(3) goals, holding the gripper unchanged.

        Native world, measured object, or measured hole frames; all poses use
        the SAME frame. Free-space movement only: no contact exemptions. Keeps
        measured held-object geometry. This does not declare grasp/placement.
        """
        return self.skill('move_tcp',target_tcp_pose=target_tcp_pose,
                          target_frame=frame,via_tcp_poses=via or [])

    def strategies(self) -> dict:
        """Describe task-relative choices; this read does not plan or authorize motion.

        Pitch is in degrees about the nominal local jaw-closing X axis at the
        same object-relative TCP. Square symmetries rotate about the measured
        object normal; no world-frame joint angles are implied. The fixture must
        enable parameterized grasp. Every choice is planned against fresh data.
        """
        return dict(geometric_tools=dict(context='robo.geometry()',preview='robo.preview(name, **parameters)',
                    grasp='grasp_tcp_pose: object-relative SE3; approach_direction_object; approach_distance_m',
                    path='move_tcp: target_tcp_pose, via_tcp_poses, target_frame world/object/hole'),
            pitch_offset_deg=dict(skills=['approach_grasp','regrasp'],minimum=-45,maximum=45,continuous=True),
            square_variant=dict(skills=['approach_grasp','regrasp','align'],values=[0,1,2,3],symmetry_degrees=90),
            transfer_variant={0:'demonstrated_transport',1:'orientation_preserving_transport',
                2:'outside_bucket_reorientation_transport',3:'orientation_preserving_other_side_transport'},
            default='automatic search; explicit choices never bypass native checks')

    def skill(self, name: str, *, pitch_offset_deg: float | None = None, variant: int | None = None,
              grasp_tcp_pose: list[float] | None = None, approach_direction_object: list[float] | None = None,
              approach_distance_m: float | None = None, target_tcp_pose: list[float] | None = None,
              via_tcp_poses: list | None = None, target_frame: str='world',
              support_contact: bool=False) -> dict:
        """Run one registered bounded skill for the prepared object/hole; inspect outcome before continuing.

        Names: approach_grasp, grasp, verify_grasp, recover_grasp, inspect_in_hand, transfer_above_hole,
        align, insert, release, release_supported, verify_placed, retreat, reobserve, regrasp.
        move_tcp accepts support_contact=True for a held-object descent to the
        registered support; release_supported separately opens and verifies
        supported release. Neither establishes insertion or overall completion.
        Optional pitch_offset_deg [-45,45] changes grasp tilt about the nominal
        jaw-closing axis at the same object-relative TCP capture point. It is
        continuous, not a fixed angle list; only approach_grasp/regrasp accept it.
        variant selects a square's 0/90/180/270-degree equivalent grasp (0..3),
        or a registered transfer/align/withdraw route. Omit both for automatic
        search. A rejected plan with no_motion=True may be changed and replanned;
        it does not mean the object is unreachable. After successful approach,
        grasp inherits the executed choice: never reset its pose independently.
        """
        if name not in SKILLS:
            raise ValueError("unregistered skill")
        parameters=MotionParameters(pitch_offset_deg=pitch_offset_deg,variant=variant,
            grasp_tcp_pose=grasp_tcp_pose,approach_direction_object=approach_direction_object,
            approach_distance_m=approach_distance_m,target_tcp_pose=target_tcp_pose,
            via_tcp_poses=via_tcp_poses or (),target_frame=target_frame,
            support_contact=support_contact).for_skill(name)
        args={"name":name}
        if parameters.supplied():
            args['parameters']=parameters.supplied()
        return self._stateful("execute_skill", args, lambda value: value)

    def resume(self, name: str, request_id: str) -> dict:
        """Replan an owner-issued stalled free-space checkpoint from fresh measurements.

        Use only the resume_request_id returned by a failed skill. The owner must
        confirm hold and remaining recovery budget. This cannot replay an unknown action.
        """
        if name not in ('approach_grasp', 'grasp', 'transfer_above_hole', 'retreat','move_tcp'):
            raise ValueError('skill_has_no_motion_resume')
        return self._stateful('execute_skill', {'name': name, 'resume_request_id': request_id}, lambda value: value)

    def recheck(self, name: str, request_id: str) -> dict:
        """Recheck fresh evidence for an owner-completed motion without moving again.

        Only use verification_request_id from pending_review. This does not
        resume an unfinished motion or replace a failed grasp with success.
        """
        if name not in SKILLS:
            raise ValueError('unregistered skill')
        return self._stateful('execute_skill', {'name': name, 'verification_request_id': request_id}, lambda value: value)


ADAPTER = RobotAdapter(name="f1", robot_cls=F1Robot, host="f1_harness.host", knowledge="f1",
                       blurb="the F1 right arm and fixed left-wrist observation camera through a locally authorized gateway",
                       services="local geometry and independently verified skill outcomes")


class _StepState:
    def latest_record(self):
        return None  # Canonical evidence belongs to F1 journal and the harness trace.


class F1Toolkit(Toolkit):
    def _register_common_tools(self):
        # RPent's generic filesystem tools are not F1 task capabilities.
        pass

    def __init__(self, backend, memory_root: Path):
        self.backend = backend
        super().__init__(dashboard_events=NullDashboardEventSink(), state=_StepState(),
                         memory=MemoryManager(memory_root, memory_access="read_only"))
        for name, handler, schema in (
            ("task_context", self.task_context, {}),
            ("aperture_observation", self.aperture_observation, {}),
            ("observe", self.observe, {"step": {"type": "integer"}}),
            ("geometry_context", self.geometry_context, {}),
            ("inspect_plan", self.inspect_plan, {"name":{"type":"string","enum":list(SKILLS)},
                                                "parameters":MotionParameters.model_json_schema()}),
            ("locate_target", self.locate_target, {"camera":{"type":"string","enum":["head","right"]},
                "box_normalized":{"type":"array","items":{"type":"number"},"minItems":4,"maxItems":4}}),
            ("locate_entities", self.locate_entities, {"camera":{"type":"string","enum":["head","right"]},
                "entities":{"type":"array","items":{"type":"object"},"minItems":1,"maxItems":8}}),
            ("preview_skill", self.preview_skill, {"name":{"type":"string","enum":list(SKILLS)},
                                                  "parameters":MotionParameters.model_json_schema()}),
            ("execute_skill", self.execute_skill, {"name": {"type": "string", "enum": list(SKILLS)},
                                                  "resume_request_id": {"type": "string"},
                                                  "parameters": MotionParameters.model_json_schema(),
                                                  "verification_request_id": {"type": "string"}}),
        ):
            self.add_tool(name, {"name": name, "description": handler.__doc__ or name,
                                "input_schema": {"type": "object", "properties": schema,
                                                 "additionalProperties": False}}, handler)

    @readonly
    def task_context(self):
        """Read prepared task metadata independently of perception."""
        return self.backend.execute_tool('task_context', {})

    @readonly
    def aperture_observation(self):
        """Inspect the current left wrist numerically without moving the robot."""
        return self.backend.execute_tool('aperture_observation', {})

    @readonly
    def inspect_plan(self,name,parameters=None):
        """Read geometry and preview from one owner observation/feedback snapshot."""
        params=MotionParameters.model_validate(parameters or {}).for_skill(name)
        return self.backend.execute_tool('inspect_plan',dict(name=name,parameters=params.supplied()))

    @readonly
    def geometry_context(self):
        """Read current calibrated planning geometry."""
        return self.backend.execute_tool('geometry_context',{})

    def locate_target(self,camera,box_normalized):
        """Update the prepared object's image search hint; does not move the robot."""
        return self.backend.execute_tool('locate_target',dict(camera=camera,box_normalized=box_normalized))

    def locate_entities(self,camera,entities):
        """Queue registered multi-object pixel hints, never a pose or motion."""
        return self.backend.execute_tool('locate_entities',dict(camera=camera,entities=entities))

    @readonly
    def preview_skill(self,name,parameters=None):
        """Check a plan without submitting movement."""
        params=MotionParameters.model_validate(parameters or {}).for_skill(name)
        return self.backend.execute_tool('preview_skill',dict(name=name,parameters=params.supplied()))

    @readonly
    def observe(self, step=-1):
        """Observe the prepared F1 task without changing physical state."""
        if step != -1:
            raise HarnessError("historical_step_not_available")
        return self.backend.execute_tool("observe", {})

    def execute_skill(self, name, resume_request_id=None, verification_request_id=None, parameters=None):
        """Execute a bounded locally authorized F1 skill and return independent evidence."""
        args = {"name": name}
        if parameters is not None:
            args['parameters']=MotionParameters.model_validate(parameters).for_skill(name).supplied()
        if resume_request_id is not None:
            args['resume_request_id'] = resume_request_id
        if verification_request_id is not None:
            args['verification_request_id'] = verification_request_id
        return self.backend.execute_tool("execute_skill", args)

    def get_env_state(self, *, command, result, elapsed_s):
        if command.get('action') in ('locate_target','locate_entities'):
            # A search-prompt mutation precedes the next usable scene. RPent
            # still records this as stateful, but an implicit observe here
            # would turn a successfully queued hint into a capture failure.
            # Keep the native receipt; do not manufacture a state envelope.
            return dict(result)
        current = self.backend.execute_tool("observe", {})
        return dict(state=current.get("observation", current), status=current.get("status", {}),
                    log={"command": command, "result": result, "elapsed_s": elapsed_s},
                    terminated=self.solved(), truncated=False)

    def solved(self):
        return self.backend.solved()

    def cancel_active_and_wait(self):
        # Bounded cancellation belongs to the gateway; do not use upstream's unbounded wait.
        return self.backend.cancel()

    def close(self):
        return self.backend.cancel()


class ScopedBackend:
    """Bind code policies to one prepared task; authority is held only by the owner."""
    def __init__(self, engine, observer, *, session_id: str, plant=None, poll_s=.01):
        self.engine, self.observer, self.session_id = engine, observer, session_id
        self.plant, self.poll_s = plant, poll_s
        self.steps = 0
        self._solved = False
        self.placement_verified = False
        self.withdraw_completed = False

    def execute_tool(self, name, input_dict):
        if name == 'aperture_observation' and not input_dict:
            measure = getattr(self.observer, 'aperture_observation', None)
            return measure() if measure is not None else dict(camera='left', available=False,
                reason='left_rgb_unavailable', candidates={}, motion_evidence=False)
        if name == 'task_context':
            if input_dict:raise HarnessError('unexpected_task_context_arguments')
            task = self.engine.task
            if task is None:raise HarnessError('task_not_prepared')
            return dict(instruction=task.operator_instruction, task_id=task.task_id,
                        task_hash=digest(task), goal=task.goal)
        if name == "observe":
            observation = self.observer.observe()
            self.engine.update_observation(observation)
            return dict(observation=observation.model_dump(exclude={
                'local_collision_evidence','local_collision_evidence_ref'}), status=self.engine.status())
        if name != "execute_skill" or 'name' not in input_dict or set(input_dict) - {'name', 'parameters', 'resume_request_id', 'verification_request_id'}:
            raise HarnessError("tool_not_available")
        self.engine.heartbeat(self.session_id)
        observation = self.observer.observe()
        self.engine.update_observation(observation)
        task, status = self.engine.task, self.engine.status()
        if task is None:
            raise HarnessError("task_not_prepared")
        intent = SkillIntent(request_id=uuid.uuid4().hex, session_id=self.session_id,
            owner_instance_id=self.engine.instance, task_id=task.task_id, task_hash=digest(task),
            epoch=status["epoch"], observation_id=observation.observation_id,
            scene_version=observation.scene_version, calibration_id=task.calibration_id,
            skill=input_dict["name"], parameters=input_dict.get('parameters',{}), resume_request_id=input_dict.get('resume_request_id'),
            verification_request_id=input_dict.get('verification_request_id'))
        self.engine.submit_skill(intent.model_dump())
        if self.plant:
            self.plant.advance()
        until = time.monotonic() + intent.budget_ms / 1000 + 1
        while True:
            result = self.engine.skill_result(self.session_id, intent.request_id)
            if result["lifecycle"] not in ("accepted", "running", "paused"):
                break
            if time.monotonic() >= until:
                result = self.engine.cancel_skill(self.session_id, intent.request_id)
                break
            self.engine.heartbeat(self.session_id)
            time.sleep(self.poll_s)
        self.steps += 1
        if intent.skill == "verify_placed" and result["outcome"] == "success" and not result.get("assisted"):
            self.placement_verified = True
        if intent.skill == "withdraw_reset" and result["outcome"] == "success" and not result.get("assisted"):
            self.withdraw_completed = True
        if "withdraw_reset" in task.allowed_skills:
            self._solved = bool(self.placement_verified and self.withdraw_completed)
        else:
            self._solved = bool(self.placement_verified)
        return result

    def solved(self):
        return self._solved

    def env_steps(self):
        return self.steps

    def artifact(self, name, step=-1):
        raise HarnessError("image_artifact_unavailable")

    def cancel(self):
        self.engine.revoke("backend_cancel")


class F1ArmToolkit(ArmToolkit):
    """Keep upstream orchestration; describe motion from F1 owner receipts."""

    def _feedback(self, before, extra):
        result = super()._feedback(before, extra)
        records = self.robo._ledger.records[before[0]:]
        from .contracts import MOTION_SKILLS
        motions = [r for r in records if r.tool == 'execute_skill'
                   and r.kwargs.get('name') in MOTION_SKILLS]
        published = [r for r in motions if r.summary.get('lifecycle') == 'completed'
                     and r.summary.get('no_motion') is False]
        unknown = [r for r in motions if r.summary.get('no_motion') is not True
                   and r not in published]
        value = result.result
        if isinstance(value, dict):
            value['moved'] = True if published else None if unknown else False
            value['motion_status_unknown'] = bool(unknown)
            value['published_skill_requests'] = len(published)
            if 'env_steps' in value:
                # Upstream counts stateful tool calls, including localization
                # and failed submissions; it is not a physical step count.
                value['tool_steps'] = value.pop('env_steps')
        return result


class ToolkitBackend:
    """Use RPent dispatch and PyRUA's backend contract without exposing file tools."""
    def __init__(self, toolkit):
        self.toolkit = toolkit

    def execute_tool(self, name, input_dict):
        return self.toolkit.execute_tool(name, input_dict)

    def solved(self):
        return self.toolkit.solved()

    def env_steps(self):
        return self.toolkit.backend.env_steps()

    def artifact(self, name, step=-1):
        return self.toolkit.backend.artifact(name, step)

    def cancel(self):
        return self.toolkit.cancel_active_and_wait()
