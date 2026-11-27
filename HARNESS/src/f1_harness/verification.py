"""Three-valued postconditions; command completion is never placement success."""
from .contracts import Observation, SkillResult, Task


SEMANTIC_PENDING_REASONS = frozenset((
    'object_not_observed', 'grasp_held_evidence_missing', 'in_hand_transform_missing',
    'placement_evidence_incomplete', 'matching_hole_evidence_missing',
    'postcondition_evidence_missing',
    'supported_release_evidence_missing', 'transport_attachment_frame_missing',
))


def needs_semantic_evidence(result: SkillResult) -> bool:
    """Unknown evidence can be re-observed, never promoted to command success."""
    return result.outcome == 'pending_review' and result.reason in SEMANTIC_PENDING_REASONS


def verify(skill: str, task: Task, observation: Observation, request_id: str) -> SkillResult:
    target = next((o for o in observation.objects if o.object_id == task.object_id), None)
    outcome, reason = "pending_review", "object_not_observed"
    if target and target.visible and target.evidence_ids and not observation.faults:
        if skill == 'transfer_above_hole' and observation.geometry_scope == 'coarse_object_verification':
            # The only witness in this scope is the object-camera RGB-D pair.
            # A newer scene revision must not re-bind historical attachment IDs.
            pair=next((p for p in observation.pairings if p.camera==observation.geometry_camera),None)
            sources={f.stream for f in observation.frames if pair and f.camera==pair.camera
                and f.sequence==(pair.rgb_sequence if f.stream=='rgb' else pair.depth_sequence)
                and f.blob_sha256 in target.evidence_ids}
            if sources!={'rgb','depth'}:
                return SkillResult(request_id=request_id,lifecycle='completed',outcome='pending_review',
                    reason='transport_attachment_frame_missing',synthetic=observation.synthetic)
        if skill in ("verify_grasp", "inspect_in_hand", "transfer_above_hole"):
            outcome = "pending_review" if target.held is None else "success" if target.held else "failure"
            reason = "grasp_held_evidence_missing" if target.held is None else "grasp_observation"
            if skill in ("inspect_in_hand", "transfer_above_hole") and target.held is True and target.eef_object_pose is None:
                outcome, reason = "pending_review", "in_hand_transform_missing"
            if skill == "transfer_above_hole" and outcome != "pending_review":
                reason = ("owner_transport_endpoint_and_held_target" if target.held
                          else "transport_target_observed_unheld")
        elif skill == "verify_placed":
            hole = next((o for o in observation.objects if o.object_id == task.hole_id), None)
            if hole is not None and hole.source == "cached_pre_transport_aperture":
                reason = "matching_hole_evidence_missing"
            elif None in (target.held, target.inserted, target.stationary):
                reason = "placement_evidence_incomplete"
            elif target.shape != task.shape:
                outcome, reason = "failure", "shape_mismatch"
            elif not target.inserted:
                outcome, reason = "failure", "not_inserted"
            elif target.inserted_hole_id is None:
                reason = "matching_hole_evidence_missing"
            elif target.inserted_hole_id != task.hole_id:
                outcome, reason = "failure", "wrong_hole"
            elif target.inserted and not target.held and target.stationary:
                outcome, reason = "success", "placement_observed"
            else:
                outcome, reason = "failure", "placement_not_confirmed"
    return SkillResult(request_id=request_id, lifecycle="completed", outcome=outcome, reason=reason,
                       evidence_ids=target.evidence_ids if target else (), synthetic=observation.synthetic)
