"""Readiness is diagnostic state, never a permission to move."""
from copy import deepcopy
import json

OBJECT_MEASUREMENT_CODES=frozenset((
    'mask_uncertain_requires_new_prompt','mask_truncated','object_mask_missing',
    'surface_points_missing_or_invalid','supported_upright_face_not_observed',
    'insufficient_upright_face_support','observed_face_is_too_narrow','ambiguous_parallel_object_faces',
    'insufficient valid depth','surface is not a reliable planar block face',
    'object_not_visible_in_either_calibrated_view'))


def object_measurement_recoverable(reason, *, camera=None, object_id=None):
    """Recognize exact measurement codes; unrelated identities cannot trigger fallback."""
    parts=str(reason).split(':')
    if parts[0].startswith('mask_view='):
        view=parts.pop(0).removeprefix('mask_view=')
        if view not in ('head','right') or camera is not None and view!=camera:return False
    if not parts or parts[0] not in OBJECT_MEASUREMENT_CODES:return False
    for part in parts[1:]:
        if camera is not None and part.startswith('camera=') and part!='camera='+camera:return False
        if object_id is not None and part.startswith('object=') and part!='object='+object_id:return False
    return True


def with_observation_details(condition,status):
    """Attach matching diagnostic data after the caller has checked authority."""
    latest=status.get('observation_readiness') or {}
    details=latest.get('details')
    context=status.get('perception_context') or {}
    if (not isinstance(details,dict) or details.get('diagnostic_only') is not True
            or details.get('reason')!=condition.get('code') or latest.get('code')!=condition.get('code')):
        return condition
    scope=details.get('scope') or {}
    if (not isinstance(scope,dict)
            or not all(scope.get(k) is not None for k in ('owner_instance_id','session_id','task_hash','epoch'))
            or any(context.get(k)!=v for k,v in scope.items())):
        return condition
    try:
        if len(json.dumps(details,allow_nan=False).encode())>12000:return condition
    except (ValueError,TypeError):return condition
    return dict(condition,details=deepcopy(details))


def compact_observation_condition(condition):
    """Keep bounded per-view numbers in a PyRUA ledger even without print()."""
    if not isinstance(condition,dict):return {}
    result={k:condition[k] for k in ('state','code','scope','recovery') if k in condition}
    details=condition.get('details')
    if not isinstance(details,dict) or details.get('diagnostic_only') is not True:return result
    brief={k:deepcopy(details[k]) for k in ('diagnostic_only','reason','observation_id','calibration_id') if k in details}
    attempts=[]
    for row in details.get('attempts',[])[:3]:
        item={k:deepcopy(row[k]) for k in ('camera','object_id','mask_available','stage','reason','metrics') if k in row}
        item['source_ns']=(deepcopy(row['source_ns']) if isinstance(row.get('source_ns'),dict) else
                          {f['stream']:f['source_ns'] for f in row.get('frames',[]) if 'source_ns' in f and 'stream' in f})
        attempts.append(item)
    brief['attempts']=attempts;result['details']=brief
    # Each surface report is small. Retain the per-view causes even if a future
    # diagnostic adds large arrays; originals remain in local scene evidence.
    try:large=len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>4000
    except (ValueError,TypeError):large=True
    if large:
        for item in attempts:item.pop('metrics',None)
        brief['metrics_omitted']='Full numeric diagnostic retained in local evidence.'
    return result


def observation_condition(reason):
    code=str(reason or 'ready')[:500]
    if code=='ready':
        return dict(state='ready',code=code,scope='observation',recovery='none')
    # Unknown and identity faults must not masquerade as ordinary missing frames.
    waiting=('scene_not_ready','scene_expired','waiting_for_new_camera_evidence',
        # Emitted only after LiveSceneObserver proves the authority scope is
        # unchanged. Discard that racing scene and wait for new evidence;
        # normal phase/request progress is not a terminal identity fault.
        'owner_perception_stage_advanced',
        'FileNotFoundError','owner_perception_context_missing','owner_perception_context_changed',
        'secondary_camera_pairing_unavailable','unverified_pairing','frame_feedback_skew',
        'feedback_expired','stationary calibrated observation required',
        'perception_latency_exceeds_observation_budget','evidence_archive_exceeds_observation_budget',
        'verification_transport_exceeds_observation_budget','primary_evidence_latency',
        'new_consistent_source_frames_required','reacquisition_requires_new_source_frames','owner_motion_wait',
        'observation_before_motion_wait_recovery','mask_worker_rejected_frame')
    sample_codes={'duplicate_source_stamp','source_time_reversed','sequence_not_increasing'}
    faults=code.removeprefix('capture_faults:').split(',') if code.startswith('capture_faults:') else []
    recoverable=(any(code==p or code.startswith(p+':') for p in waiting)
                 or bool(faults) and all(f.split(':')[0] in sample_codes for f in faults))
    # A rejected perception sample is not a changed camera/calibration identity.
    # Await another measured sample within the existing task deadline. The bad
    # sample remains invalid and can never be used to authorize motion.
    quality_code=code.split(':',1)[1] if code.startswith(('mask_view=head:','mask_view=right:')) else code
    sample_quality=('object_identity_unresolved',
        'fixture_aperture_not_detected_in_search_region','fixture_relocalization_anchor_missing',
        'fixture_relocalization_depth_missing','fixture_relocalization_lid_not_planar',
        'fixture_relocalization_ambiguous_aperture','fixture_relocalization_aperture_not_square')
    recoverable=(recoverable or object_measurement_recoverable(code)
                 or any(quality_code==p or quality_code.startswith(p+':') for p in sample_quality))
    scope='observation'
    if faults and len({f.rsplit(':',1)[-1] for f in faults})==1:
        scope='camera:'+faults[0].rsplit(':',1)[-1]
    recovery=('await_forward_sample_or_acquisition_epoch' if 'source_time_reversed' in code
              else 'await_new_evidence' if recoverable else 'resolve_identity_or_scene')
    return dict(state='waiting' if recoverable else 'needs_attention',code=code,scope=scope,recovery=recovery)
