"""Closed recovery IR compiled to PyRUA helpers; neither notes nor arbitrary code execute."""
from copy import deepcopy
from .contracts import HarnessError,digest


PROGRAMS = {
    'verify_completed_motion': dict(schema='f1-recovery-program-v1',
        trigger=dict(outcome='pending_review',required_result_fields=['verification_request_id']),
        operation='recheck_same_skill',request_field='verification_request_id',
        authority='owner_verification_checkpoint',budget='task.local_corrections',
        motion_replayed=False),
    'resume_known_progress': dict(schema='f1-recovery-program-v1',
        trigger=dict(outcome='failure',reason='motion_stalled',cancel_stage='measured_stationary',
                     required_result_fields=['resume_request_id']),
        operation='resume_same_skill',request_field='resume_request_id',
        authority='owner_motion_checkpoint',budget='task.local_corrections',
        grasp_constraint='owner_proven_preclosure_only'),
    'insert_reobserve_realign': dict(schema='f1-recovery-program-v1',
        trigger=dict(skill='insert',outcome='failure'),
        operation='registered_skill_sequence',skills=['retreat','reobserve','align','insert'],
        authority='prepared_task_and_current_geometry',budget='task.local_corrections'),
}


def registered_program(name):
    if name not in PROGRAMS:raise HarnessError('unregistered_recovery_program')
    return dict(id=name,**deepcopy(PROGRAMS[name]))


def validate_program(program):
    if not isinstance(program,dict) or program!=registered_program(program.get('id')):
        raise HarnessError('recovery_program_changed_or_unregistered')
    return digest(program)


def matches(program,skill,result):
    validate_program(program)
    trigger=program['trigger']
    return (all(result.get(key)==value for key,value in trigger.items()
                if key not in ('required_result_fields','skill'))
            and ('skill' not in trigger or trigger['skill']==skill)
            and all(isinstance(result.get(key),str) and result[key]
                    for key in trigger.get('required_result_fields',[])))


def compile_library(local_corrections, regrasp_limit=0, *, grasp_observation_enabled=False):
    """Compiler for the registered baseline recovery IR, never a Markdown eval.

    These are existing built-in recoveries, not a promoted learned strategy.
    Runtime checkpoint scope, budgets and the actual hardware predicates remain
    the owner authority. A client match is only eligibility to request a check.
    """
    from .engine import VERIFICATION_RECHECK_LIMIT

    if type(local_corrections) is not int or not 0<=local_corrections<=2:
        raise HarnessError('invalid_registered_recovery_budget')
    if type(regrasp_limit) is not int or not 0<=regrasp_limit<=1:
        raise HarnessError('invalid_registered_regrasp_budget')
    if type(grasp_observation_enabled) is not bool:
        raise HarnessError('invalid_grasp_observation_enable')
    identities={name:validate_program(registered_program(name)) for name in PROGRAMS}
    code='RECOVERY_PROGRAM_IDS = '+repr(identities)+'\n'
    code+='VERIFICATION_RECHECK_LIMIT = '+str(VERIFICATION_RECHECK_LIMIT)+'\n'
    code+='MOTION_PREFERENCES = {}\n'
    code+='OBSERVATION_VIEWS_TRIED = set()\n'
    code+='PLACEMENT_VIEW = {}\n'
    code+='GRASP_OBSERVATION_ENABLED = '+repr(grasp_observation_enabled)+'\n'
    code+='INSERTION_RECOVERY = '+repr(tuple(registered_program('insert_reobserve_realign')['skills'][:-1]))+'\n'
    code+='''
def unknown_grasp(result):
    return (result.get("outcome") == "pending_review" and result.get("reason") in (
        "grasp_held_evidence_missing",
        "post_motion_verification_unavailable:grasp_held_evidence_missing"))

def replaceable_candidate(result):
    # Only the original owner receipt can certify no physical attempt. The
    # next candidate must still have different parameters and current checks.
    return (result.get("no_motion") is True
        and result.get("lifecycle") in ("completed", "rejected")
        and result.get("outcome") in ("failure", "pending_review")
        and not result.get("resume_request_id") and not result.get("verification_request_id")
        and (result.get("retryable") is True
             or result.get("reason") == "numerical_plan_rejected:registered_candidates_exhausted"))

def evidence(name, result):
    for _ in range(VERIFICATION_RECHECK_LIMIT):
        checkpoint = result.get("verification_request_id")
        if result["outcome"] != "pending_review" or not checkpoint: break
        result = robo.recheck(name, checkpoint)
        if result.get("continuation_submitted") is False: break
    return result

def skill(name, **parameters):
    selected = dict(MOTION_PREFERENCES.get(name, {}))
    selected.update(parameters)
    result = robo.skill(name, **selected)
    if not (GRASP_OBSERVATION_ENABLED and name in ("verify_grasp", "recover_grasp") and unknown_grasp(result)):
        result = evidence(name, result)
    if selected and not parameters and replaceable_candidate(result):
        result = evidence(name, robo.skill(name))
    return result

def continue_motion(name, result):
    for _ in range(BUDGET):
        checkpoint = result.get("resume_request_id")
        if result["outcome"] != "failure" or not checkpoint: break
        if result.get("reason") != "motion_stalled": return result
        if result.get("cancel_stage") != "measured_stationary": return result
        result = evidence(name, robo.resume(name, checkpoint))
        if result.get("continuation_submitted") is False:
            view = (result.get("planning_diagnostic") or {}).get("observation_view")
            if (name != "approach_grasp" or not view or checkpoint in OBSERVATION_VIEWS_TRIED
                    or result.get("resume_request_id") != checkpoint
                    or result.get("observation_retry_reason") !=
                       "observation_viewpoint_required:local_environment_only_supports_world_observation_motion"):
                return result
            # Before this approach the wrist observed the target at this TCP.
            # Replan to that view from live feedback. Never replay the path or
            # treat either the old target estimate or this preview as fresh.
            OBSERVATION_VIEWS_TRIED.add(checkpoint)
            observed = move_and_reobserve([dict(target_frame="world", target_tcp_pose=view["target_tcp_pose"])])
            if (observed.get("reason") == "local_motion_candidates_exhausted"
                    and observed.get("no_motion") is True):
                # A rejected view is not evidence that the object vanished.
                # Get one new stationary measurement before requesting fresh
                # segmentation hints. This branch never commands a blind move.
                refresh = skill("reobserve")
                observed["stationary_reobservation"] = refresh
                if refresh.get("outcome") == "success":
                    geometry = robo.geometry()
                    observed["current_observation_id"] = geometry.get("observation_id")
                    observed["missing_entities"] = geometry.get("missing_entities")
                    observed["object_pose_recovered"] = geometry.get("object_pose") is not None
            if observed.get("outcome") == "success" or observed.get("object_pose_recovered") is True:
                resumed = evidence(name, robo.resume(name, checkpoint))
                return dict(resumed, observation_recovery=observed)
            return dict(result, observation_recovery=observed, needs_new_strategy=True,
                        recovery_hint="localize_target_from_new_evidence_then_resume_original_checkpoint")
    return result

def motion(name, **parameters):
    return continue_motion(name, skill(name, **parameters))

def move_and_reobserve(candidates):
    """Try distinct Astra-proposed move_tcp goals; after one success reobserve.

    Each candidate is a complete independently checked local movement, never
    an unchecked prefix. No task-specific pose, distance or direction is added.
    A failed observation preserves the successful movement receipt.
    """
    if not isinstance(candidates, (list, tuple)) or not 1 <= len(candidates) <= 6:
        raise ValueError("provide one to six local move_tcp parameter dictionaries")
    if any(not isinstance(c, dict) for c in candidates):
        raise ValueError("local motion candidates must be parameter dictionaries")
    tried = []
    attempts = []
    scene_refreshed = False
    for parameters in candidates:
        if parameters in tried: continue
        tried.append(dict(parameters))
        preview = robo.preview("move_tcp", **parameters)
        attempts.append(dict(parameters=parameters, preview=preview))
        if preview.get("current_scope") is not True:
            return dict(outcome="pending_review", reason="local_motion_scope_changed",
                        no_motion=True, attempts=attempts)
        missing_scene = (preview.get("planning_diagnostic") or {}).get("code") in (
            "full_scene_required_for_motion", "missing_geometry_entities", "observation_unavailable")
        if missing_scene and not scene_refreshed:
            # A missing observation says nothing about this candidate's path.
            # Reuse the same bounded acquisition as grasp candidate search,
            # refreshing this candidate once for the entire helper call.
            scene_refreshed = True
            inspected = robo.inspect_plan("move_tcp", **parameters)
            preview = inspected.get("preview") or {}
            attempts[-1].update(preview=preview, scene_refreshed=True)
            if preview.get("current_scope") is not True:
                return dict(outcome="pending_review", reason="local_motion_scope_changed",
                            no_motion=True, attempts=attempts)
            missing_scene = (preview.get("planning_diagnostic") or {}).get("code") in (
                "full_scene_required_for_motion", "missing_geometry_entities", "observation_unavailable")
        if missing_scene:
            return dict(outcome="pending_review", reason="local_motion_scene_unavailable",
                        no_motion=True, attempts=attempts,
                        planning_diagnostic=preview.get("planning_diagnostic"),
                        recovery_hint="refresh_scene")
        if preview.get("planned") is not True:
            continue
        original = robo.skill("move_tcp", **parameters)
        attempts[-1]["motion"] = original
        if replaceable_candidate(original):
            continue
        result = continue_motion("move_tcp", evidence("move_tcp", original))
        if result.get("outcome") != "success":
            return dict(result, program_stage="move_tcp", attempts=attempts)
        observation = skill("reobserve")
        return dict(observation, program_stage="reobserve", motion_result=result,
                    local_motion_completed=True, no_motion=False, attempts=attempts)
    return dict(outcome="pending_review", reason="local_motion_candidates_exhausted",
                no_motion=True, attempts=attempts, needs_new_strategy=True)

def recover_attachment(result, withdraw=True):
    if (result.get("reason") != "attachment_lost" or result.get("outcome") != "failure"
            or result.get("cancel_stage") != "measured_stationary"):
        return result
    # Owner verifies a current stationary target; the planner verifies support
    # and the complete approach. The existing task regrasp budget is authority.
    for _ in range(REGRASP_BUDGET):
        refreshed = skill("reobserve")
        if refreshed["outcome"] != "success":
            return dict(refreshed, program_stage="reobserve", prior_result=result,
                        recovery_hint="refresh_attachment_evidence")
        recovery = motion("regrasp")
        if recovery["outcome"] != "success": return dict(recovery, program_stage="regrasp")
        return grasp_and_place(withdraw=withdraw, start="verify_grasp")
    return result

def grasp_and_place(withdraw=True, *, start="grasp"):
    regrasp_used = False
    stages = ("grasp", "verify_grasp")
    for name in stages[stages.index(start):]:
        result = motion(name) if name == "grasp" else skill(name)
        if (name == "verify_grasp" and GRASP_OBSERVATION_ENABLED
                and unknown_grasp(result)):
            result = motion("recover_grasp")
            if result["outcome"] == "success":
                result = skill("verify_grasp")
            elif not (result["outcome"] == "failure" and
                      (result.get("reason") == "grasp_observation" or
                       result.get("reason") == "grasp_not_attached" and
                       result.get("cancel_stage") == "measured_stationary")):
                return dict(result, program_stage="recover_grasp")
        known_miss = (name == "verify_grasp" and
                      (result.get("reason") == "grasp_observation" or
                       result.get("reason") == "grasp_not_attached" and
                       result.get("cancel_stage") == "measured_stationary") or
                      name == "grasp" and result.get("reason") == "grasp_not_attached" and
                      result.get("cancel_stage") == "measured_stationary")
        if not regrasp_used and result["outcome"] == "failure" and known_miss:
            for _ in range(REGRASP_BUDGET):
                regrasp_used = True
                # The miss is historical evidence, not permission to reopen.
                # Refresh after measured hold; the owner still requires current
                # held=False before planning any regrasp/opening motion.
                refreshed = skill("reobserve")
                if refreshed["outcome"] != "success":
                    return dict(refreshed, program_stage="reobserve", prior_result=result,
                                recovery_hint="refresh_attachment_evidence")
                recovery = motion("regrasp")
                if recovery["outcome"] != "success":
                    return dict(recovery, program_stage="regrasp")
                result = recovery if name == "grasp" else skill("verify_grasp")
        if result["outcome"] != "success":
            return dict(result, program_stage=name)
    return place_held_block(withdraw=withdraw)

def pick_and_place(withdraw=True, **approach_parameters):
    result = motion("approach_grasp", **approach_parameters)
    if result["outcome"] != "success":
        return dict(result, program_stage="approach_grasp")
    return grasp_and_place(withdraw=withdraw)

def localize_and_pick(hints_by_camera, withdraw=True, **approach_parameters):
    # Explicit execution entry: the owner acquires, plans and revalidates the
    # approach before publishing. An extra identical preview is unnecessary.
    # Keep previews for strategy comparison; hints alone never authorize motion.
    if not isinstance(hints_by_camera, dict) or any(
            not isinstance(camera, str) or not isinstance(entities, list)
            or any(not isinstance(entity, dict) for entity in entities)
            for camera, entities in hints_by_camera.items()):
        raise ValueError("provide camera names mapped to visible entity hints")
    for camera, entities in hints_by_camera.items():
        if entities:
            localization = robo.locate_entities(camera, entities)
            if localization.get("accepted") is not True:
                return dict(outcome="pending_review", program_stage="localize", no_motion=True,
                            reason=localization.get("reason"), localization=localization)
    result = motion("approach_grasp", **approach_parameters)
    if result.get("outcome") == "success":
        return grasp_and_place(withdraw=withdraw)
    if replaceable_candidate(result):
        geometry = robo.geometry()
        candidates = (geometry.get("grasp_suggestions") or {}).get("candidates", [])
        parameters = [c["parameters"] for c in candidates if isinstance(c, dict) and isinstance(c.get("parameters"), dict)]
        if parameters:
            return pick_with_candidates(parameters[:6], withdraw=withdraw)
    return dict(result, program_stage="approach_grasp")

def pick_with_candidates(candidates, withdraw=True):
    if not isinstance(candidates, (list, tuple)) or not 1 <= len(candidates) <= 6:
        raise ValueError("provide one to six distinct grasp parameter dictionaries")
    if any(not isinstance(c, dict) for c in candidates):
        raise ValueError("grasp candidates must be parameter dictionaries")
    tried = []
    attempts = []
    scene_refreshed = False
    last = dict(outcome="pending_review", reason="grasp_candidate_search_exhausted", no_motion=True)
    for index, candidate in enumerate(candidates):
        if candidate in tried: continue
        tried.append(dict(candidate))
        preview = robo.preview("approach_grasp", **candidate)
        attempt = dict(index=index, planned=preview.get("planned"), submitted=False)
        attempts.append(attempt)
        if preview.get("current_scope") is not True:
            return dict(outcome="pending_review", reason="grasp_candidate_preview_scope_changed",
                        no_motion=True, program_stage="approach_grasp", candidate_search=attempts)
        missing_scene = (preview.get("planning_diagnostic") or {}).get("code") in (
            "full_scene_required_for_motion", "missing_geometry_entities", "observation_unavailable")
        if missing_scene and not scene_refreshed:
            # Missing scene evidence does not reject this grasp or justify
            # trying every other pose against the same incomplete scene.
            # Refresh this same candidate once within the existing task budget.
            scene_refreshed = True
            inspected = robo.inspect_plan("approach_grasp", **candidate)
            preview = inspected.get("preview") or {}
            attempt.update(planned=preview.get("planned"), scene_refreshed=True)
            if preview.get("current_scope") is not True:
                return dict(outcome="pending_review", reason="grasp_candidate_preview_scope_changed",
                            no_motion=True, program_stage="approach_grasp", candidate_search=attempts)
            missing_scene = (preview.get("planning_diagnostic") or {}).get("code") in (
                "full_scene_required_for_motion", "missing_geometry_entities", "observation_unavailable")
        if missing_scene:
            return dict(outcome="pending_review", reason="grasp_candidate_scene_unavailable",
                        no_motion=True, program_stage="approach_grasp", candidate_search=attempts,
                        planning_diagnostic=preview.get("planning_diagnostic"),
                        recovery_hint="refresh_scene")
        if preview.get("planned") is not True:
            last = dict(outcome="pending_review", reason="grasp_candidate_search_exhausted", no_motion=True,
                        planning_diagnostic=preview.get("planning_diagnostic"))
            continue
        # Decide replacement from the original owner reply, before any resume
        # or evidence continuation. A later zero-output resume failure cannot
        # prove that the original approach published nothing.
        result = robo.skill("approach_grasp", **candidate)
        attempt.update(submitted=True, request_id=result.get("request_id"), reason=result.get("reason"))
        if replaceable_candidate(result):
            last = result
            continue
        result = continue_motion("approach_grasp", evidence("approach_grasp", result))
        if result["outcome"] != "success":
            return dict(result, program_stage="approach_grasp", candidate_search=attempts)
        result = grasp_and_place(withdraw=withdraw)
        return dict(result, candidate_search=attempts)
    return dict(last, program_stage="approach_grasp", candidate_search=attempts,
                candidate_search_exhausted=True)

def unsubmitted_alignment_observation(result):
    reason = result.get("reason") or ""
    return (result.get("no_motion") is True and result.get("lifecycle") in ("rejected", "completed")
            and result.get("outcome") in ("failure", "pending_review")
            and not result.get("resume_request_id") and not result.get("verification_request_id")
            and (reason.startswith(("observation_readiness_timeout:", "aperture_measurement_required:",
                                    "observation_viewpoint_required:"))
                 or (result.get("planning_diagnostic") or {}).get("code") in
                    ("missing_geometry_entities", "full_scene_required_for_motion")))

def aperture_facts(inspected):
    geometry = inspected.get("geometry") or {}
    entities = geometry.get("entities") or []
    target = next((row for row in entities if row.get("role") == "target_object"), {})
    aperture = next((row for row in entities if row.get("role") == "aperture"), {})
    return dict(observation_id=geometry.get("observation_id"),
                measurement_received_ns=geometry.get("measurement_received_ns"),
                current_scope=inspected.get("current_scope"), planned=inspected.get("planned"),
                held=target.get("held"), in_hand_transform=target.get("in_hand_transform_available"),
                aperture_id=aperture.get("object_id"), aperture_measured=aperture.get("has_pose"),
                aperture_visible=aperture.get("visible"),
                fixture_collision_geometry=geometry.get("fixture_collision_geometry_available"),
                missing=geometry.get("missing_entities") or inspected.get("missing_entities") or [],
                diagnostic=(inspected.get("planning_diagnostic") or {}).get("code"))

def recover_alignment(result, inspected=None):
    if not unsubmitted_alignment_observation(result): return result
    if inspected is None: inspected = robo.inspect_plan("align")
    before = aperture_facts(inspected)
    if before["current_scope"] is True and before["planned"] is True:
        return skill("align")
    facts = dict(original_align_request=result.get("request_id"),
                 original_align_reason=result.get("reason"), before=before)
    view = inspected.get("left_aperture_observation") or {}
    if view:
        facts["left_camera"] = dict(available=view.get("available"), frame=view.get("frame"),
            source_ns=view.get("source_ns"), motion_evidence=view.get("motion_evidence"),
            centers_px={shape: [item.get("center_px") for item in items]
                        for shape, items in (view.get("candidates") or {}).items()})
    candidate = view.get("observation_move") or {}
    attempted_motion = False
    if (before["current_scope"] is True and before["held"] is True and before["in_hand_transform"] is True
            and any(row.get("role") == "aperture" for row in before["missing"])
            and candidate.get("source_observation_id") and candidate.get("target_frame") == "world"
            and candidate.get("target_tcp_pose") and (not PLACEMENT_VIEW or
                PLACEMENT_VIEW.get("missing_fixture") and before["fixture_collision_geometry"] is True)):
        for _ in range(BUDGET):
            PLACEMENT_VIEW["source_observation_id"] = candidate["source_observation_id"]
            PLACEMENT_VIEW["missing_fixture"] = False
            parameters = dict(target_frame="world", target_tcp_pose=candidate["target_tcp_pose"])
            observed = move_and_reobserve([parameters])
            facts["view"] = dict(source_observation_id=candidate["source_observation_id"],
                target_tcp_pose=parameters["target_tcp_pose"],
                motion_request_id=(observed.get("motion_result") or {}).get("request_id"),
                observation_request_id=observed.get("request_id"), outcome=observed.get("outcome"),
                reason=observed.get("reason"), local_motion_completed=observed.get("local_motion_completed"),
                no_motion=observed.get("no_motion"))
            for attempt in observed.get("attempts") or []:
                diagnostic = (attempt.get("preview") or {}).get("planning_diagnostic") or {}
                facts["view"]["preview_diagnostic"] = diagnostic.get("code")
            if observed.get("program_stage") == "move_tcp" and observed.get("outcome") != "success":
                # This receipt can represent partial/unknown movement. Preserve its
                # owner checkpoint and stage rather than restarting align or a view.
                return dict(observed, placement_recovery=facts, resume_placement_from="align")
            attempted_motion = observed.get("local_motion_completed") is True
            if (observed.get("reason") == "local_motion_scene_unavailable" and observed.get("no_motion") is True
                    and before["fixture_collision_geometry"] is False):
                PLACEMENT_VIEW["missing_fixture"] = True
            if (observed.get("outcome") != "success" and observed.get("no_motion") is not True
                    and (not attempted_motion or observed.get("outcome") == "interrupted"
                         or observed.get("verification_request_id") or observed.get("resume_request_id"))):
                return dict(observed, placement_recovery=facts, resume_placement_from="align")
            if attempted_motion and observed.get("outcome") == "success":
                measured = observed.get("alignment_observation") or {}
                if (measured.get("hole_id") == before.get("aperture_id")
                        and measured.get("observation_id") and measured["observation_id"] != before["observation_id"]
                        and before["measurement_received_ns"] is not None
                        and measured["received_ns"] > before["measurement_received_ns"]):
                    return skill("align")
                after = aperture_facts(robo.inspect_plan("align"))
                facts["after"] = after
                if recovered_aperture(before, after):
                    return skill("align")
            break
    facts["view_source_tried"] = PLACEMENT_VIEW.get("source_observation_id")
    facts["view_reason"] = view.get("observation_move_reason") or view.get("reason")
    latest = facts.get("after") or before
    if latest["current_scope"] is not True:
        facts["next"] = "Check current owner scope and feedback before resuming alignment."
    elif latest["held"] is not True or latest["in_hand_transform"] is not True:
        facts["next"] = "Recover current attachment evidence before resuming alignment."
    elif (latest["fixture_collision_geometry"] is False and
            (facts.get("view") or {}).get("reason") == "local_motion_scene_unavailable"):
        facts["next"] = "Recover a current fixture collision reference for the held viewpoint move. Resume at align."
    elif latest["aperture_measured"] is True and latest["planned"] is not True:
        facts["next"] = "Choose a new alignment approach from the current aperture measurement and native diagnostic."
    else:
        facts["next"] = "Choose a distinct observation to recover current aperture geometry, then resume at align."
    # Include the compact handoff even when a caller prints only its last stage.
    print(dict(placement_handoff=facts))
    return dict(outcome="pending_review", reason="alignment_observation_unresolved",
                no_motion=False if attempted_motion else result.get("no_motion"),
                program_stage="align", needs_new_strategy=True, placement_recovery=facts, unsubmitted_align=result)

def recovered_aperture(before, after):
    return (after["current_scope"] is True and after["planned"] is True
            and after["aperture_measured"] is True and after["aperture_visible"] is True
            and after["held"] is True and after["in_hand_transform"] is True
            and after["aperture_id"] == before.get("aperture_id")
            and after["observation_id"] and after["observation_id"] != before.get("observation_id")
            and before.get("measurement_received_ns") is not None and after["measurement_received_ns"] is not None
            and after["measurement_received_ns"] > before["measurement_received_ns"])

def place_held_block(withdraw=True, *, start="transfer_above_hole"):
    stages = ("transfer_above_hole", "inspect_in_hand", "align", "insert", "release", "verify_placed", "withdraw_reset")
    offset = stages.index(start)
    for name in stages[offset:2]:
        result = motion(name)
        if result["outcome"] != "success":
            return recover_attachment(dict(result, program_stage=name), withdraw=withdraw)
        if name == "transfer_above_hole": PLACEMENT_VIEW.clear()
    if offset <= 2:
        result = recover_alignment(skill("align"))
        if result["outcome"] != "success":
            return recover_attachment(dict(result, program_stage=result.get("program_stage", "align")), withdraw=withdraw)
    stage = "insert"
    result = skill("insert") if offset <= 3 else dict(outcome="success")
    for _ in range(BUDGET):
        if result["outcome"] == "success": break
        if result.get("reason") == "attachment_lost":
            return recover_attachment(dict(result, program_stage=stage), withdraw=withdraw)
        if result["outcome"] != "failure": return dict(result, program_stage=stage)
        for name in INSERTION_RECOVERY:
            result = motion(name)
            if name == "align": result = recover_alignment(result)
            if result["outcome"] != "success": return dict(result, program_stage=result.get("program_stage", name))
        result = skill("insert")
        stage = "insert"
    if result["outcome"] != "success": return recover_attachment(dict(result, program_stage=stage), withdraw=withdraw)
    for name in stages[max(offset,4):6]:
        result = skill(name)
        if result["outcome"] != "success": return dict(result, program_stage=name)
    return (dict(motion("withdraw_reset"), program_stage="withdraw_reset") if withdraw
            else dict(result, program_stage="verify_placed"))

def continue_external_task(withdraw=True):
    # Read the owner's scoped handoff receipt. Never manufacture successful
    # grasp/release receipts for actions performed by another implementation.
    observed = robo.observe()
    status = observed.get("status") or {}
    external = status.get("external_execution") or {}
    if not external.get("motion_ended") or not external.get("old_actions_discarded"):
        return dict(outcome="pending_review", reason="external_handoff_not_proven", no_motion=True)
    if external.get("released"):
        # The VLA may already have placed it. Verify before any new pickup.
        result = skill("verify_placed")
        if result["outcome"] != "success": return dict(result, program_stage="verify_placed")
        return (dict(motion("withdraw_reset"), program_stage="withdraw_reset") if withdraw else result)
    if external.get("closed"):
        # The same measured verifier, observation recovery and regrasp budget
        # used after a geometric grasp also handles VLA grasps and misses.
        return grasp_and_place(withdraw=withdraw, start="verify_grasp")
    return dict(outcome="pending_review", reason="external_execution_needs_new_approach", no_motion=True)

def resume_pick(result, hints_by_camera=None, withdraw=True):
    # Only original owner checkpoints or explicit zero-motion rejections can
    # restart a stage. Rechecking never repeats the completed physical action.
    stage = result.get("program_stage")
    stages = ("approach_grasp", "grasp", "regrasp", "verify_grasp", "recover_grasp",
              "transfer_above_hole", "inspect_in_hand", "align", "insert",
              "release", "verify_placed", "withdraw_reset")
    if stage not in stages: return result
    if result.get("reason") == "attachment_lost":
        return recover_attachment(result, withdraw=withdraw)
    if hints_by_camera:
        for camera, entities in hints_by_camera.items():
            if entities:
                localization = robo.locate_entities(camera, entities)
                if localization.get("accepted") is not True:
                    return dict(result, localization=localization)
    checked = result
    if (stage == "align" and result.get("reason") == "alignment_observation_unresolved"
            and (result.get("placement_recovery") or {}).get("original_align_request")):
        # A repaired registration can join the unsubmitted alignment directly.
        # A read-only preview still has to confirm the current owner/scene.
        inspected = robo.inspect_plan("align")
        recovery = result["placement_recovery"]
        current = aperture_facts(inspected)
        moved = (recovery.get("view") or {}).get("local_motion_completed") is True
        if (current["planned"] is True and current["current_scope"] is True
                and (not moved or recovered_aperture(recovery.get("before") or {}, current))):
            return place_held_block(withdraw=withdraw, start="align")
        view_available = (inspected.get("left_aperture_observation") or {}).get("observation_move")
        if (current["current_scope"] is True and view_available and result.get("unsubmitted_align")
                and (not PLACEMENT_VIEW or PLACEMENT_VIEW.get("missing_fixture")
                     and current["fixture_collision_geometry"] is True)):
            aligned = recover_alignment(result["unsubmitted_align"], inspected)
            if aligned["outcome"] != "success":
                return dict(aligned, program_stage=aligned.get("program_stage", "align"))
            return place_held_block(withdraw=withdraw, start="insert")
        return dict(result, current_geometry=current)
    if result.get("verification_request_id"):
        checked = evidence(stage, result)
    elif result.get("resume_request_id"):
        checked = continue_motion(stage, result)
    elif replaceable_candidate(result):
        checked = motion(stage)
    else: return result
    if unknown_grasp(checked) and stage in ("verify_grasp", "recover_grasp"):
        return grasp_and_place(withdraw=withdraw, start="verify_grasp")
    if checked.get("outcome") != "success": return dict(checked, program_stage=stage)
    if stage == "approach_grasp": return grasp_and_place(withdraw=withdraw)
    if stage in ("grasp", "regrasp", "recover_grasp"):
        return grasp_and_place(withdraw=withdraw, start="verify_grasp")
    if stage == "verify_grasp": return place_held_block(withdraw=withdraw)
    if stage == "withdraw_reset": return dict(checked, program_stage=stage)
    return place_held_block(withdraw=withdraw, start=stages[stages.index(stage)+1])
'''.replace('range(BUDGET)','range('+str(local_corrections)+')').replace('range(REGRASP_BUDGET)','range('+str(regrasp_limit)+')')
    return code
