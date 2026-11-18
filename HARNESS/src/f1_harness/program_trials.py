"""Execution-before-motion evidence for discovery and frozen-program trials.

A new owner run establishes independent execution, not a new physical layout.
The original observation, including unknown source times, remains inspectable.
"""
import re

from .contracts import HarnessError, Observation, Task, digest


TRIAL_SCHEMA = 'f1-agent-program-trial-v1'
TRIAL_SCOPE = ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'epoch', 'calibration_id')
CANDIDATE_FIELDS = ('candidate_id', 'source_sha256', 'discovery_run', 'discovery_scene',
                    'discovery_scenario', 'discovery_source_sha256', 'discovery_scope')


def _scope(value):
    if not isinstance(value, dict) or set(value) != set(TRIAL_SCOPE):
        raise HarnessError('program_trial_scope_required')
    if (type(value['epoch']) is not int or value['epoch'] < 0
            or any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 256
                   for k in TRIAL_SCOPE if k != 'epoch')):
        raise HarnessError('program_trial_scope_required')
    return dict(value)


def trial_candidate(record, candidate_id):
    """Extract the fixed discovery reference; no source text is executed here."""
    return _candidate(dict(candidate_id=candidate_id,
        source_sha256=record['agent_program']['source_sha256'],
        **{key:record[key] for key in CANDIDATE_FIELDS if key not in ('candidate_id', 'source_sha256')}))


def _candidate(value):
    if value is None:return None
    if not isinstance(value, dict) or set(value) != set(CANDIDATE_FIELDS):
        raise HarnessError('program_trial_candidate_identity_required')
    if any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 4096
           for k in CANDIDATE_FIELDS if k != 'discovery_scope'):
        raise HarnessError('program_trial_candidate_identity_required')
    if any(not re.fullmatch('[a-f0-9]{64}', value[k])
           for k in ('candidate_id','source_sha256','discovery_source_sha256')):
        raise HarnessError('program_trial_candidate_identity_required')
    return dict(value, discovery_scope=_scope(value['discovery_scope']))


def _scene_content(observation):
    # Byte identities are evidence identity, never a claim of layout diversity.
    # Strip timestamps, acquisition IDs and evidence references so changing only
    # a new-run label or clock cannot manufacture different scene content.
    value=observation.model_dump(mode='json')
    frames=[{k:f[k] for k in ('camera','stream','frame_id','width','height','encoding',
                             'blob_sha256','depth_scale_m','camera_info_sha256')}
            for f in value['frames']]
    objects=[{k:v for k,v in item.items() if k != 'evidence_ids'} for item in value['objects']]
    fixture=value['fixture']
    if fixture is not None:
        fixture={k:v for k,v in fixture.items() if k not in ('evidence_ids','measured_at_ns')}
    return dict(calibration_id=value['calibration_id'],frames=frames,objects=objects,fixture=fixture,
                state16=value['state16'],state_valid_mask=value['state_valid_mask'],
                geometry_scope=value['geometry_scope'],geometry_camera=value['geometry_camera'])


def build_program_trial(observation, task, *, scope, owner_run_id, candidate=None):
    """Build from the owner's current observation before any accepted action."""
    obs=Observation.model_validate(observation)
    task=Task.model_validate(task)
    scope=_scope(scope); candidate=_candidate(candidate)
    if (not isinstance(owner_run_id, str) or not owner_run_id or len(owner_run_id)>256
            or scope['task_id'] != task.task_id or scope['task_hash'] != digest(task)
            or scope['calibration_id'] != task.calibration_id
            or obs.calibration_id != task.calibration_id or not obs.frames
            or {f.stream for f in obs.frames} != {'rgb','depth'}):
        raise HarnessError('program_trial_initial_observation_required')
    if candidate is not None and all(scope[k] == candidate['discovery_scope'][k]
                                    for k in ('owner_instance_id','session_id','epoch')):
        raise HarnessError('independent_validation_required')
    raw=obs.model_dump(mode='json')
    value=dict(schema=TRIAL_SCHEMA,mode='validation' if candidate else 'discovery',
        owner_run_id=owner_run_id,scope=scope,task=task.model_dump(mode='json'),
        observation=raw,observation_sha256=digest(raw),
        scene_content_sha256=digest(_scene_content(obs)),
        scenario_id=digest(dict(owner_run_id=owner_run_id,scope=scope)),candidate=candidate,
        synthetic=obs.synthetic,heldout=candidate is not None,
        validation_scope='independent_run' if candidate else None,generalization_proven=False)
    return dict(value,manifest_sha256=digest(value))


def validate_program_trial(trial):
    if not isinstance(trial, dict):raise HarnessError('program_trial_manifest_invalid')
    try:
        expected=build_program_trial(trial['observation'],trial['task'],scope=trial['scope'],
            owner_run_id=trial['owner_run_id'],candidate=trial['candidate'])
    except (KeyError,TypeError,ValueError) as exc:
        raise HarnessError('program_trial_manifest_invalid') from exc
    if expected != trial:raise HarnessError('program_trial_manifest_changed')
    return expected


def owner_program_trial(rows):
    """Validate an additive owner journal event without rewriting run_identity."""
    entries=[(i,r) for i,r in enumerate(rows) if r.get('kind')=='program_trial_frozen']
    if not entries:return None
    if len(entries)!=1:raise HarnessError('program_trial_owner_identity_ambiguous')
    index,event=entries[0]; trial=validate_program_trial(event.get('trial'))
    stamp=event.get('monotonic_ns'); scope=trial['scope']
    if type(stamp) is not int or stamp<0:raise HarnessError('program_trial_order_unproven')
    prepared=[(i,r) for i,r in enumerate(rows) if r.get('kind')=='task_prepared'
              and r.get('task_hash')==scope['task_hash']]
    if (len(prepared)!=1 or prepared[0][0]>=index
            or prepared[0][1].get('task')!=trial['task']
            or prepared[0][1].get('owner_instance_id')!=scope['owner_instance_id']
            or prepared[0][1].get('session_id')!=scope['session_id']
            or type(prepared[0][1].get('monotonic_ns')) is not int
            or prepared[0][1]['monotonic_ns']>=stamp):
        raise HarnessError('program_trial_task_binding_missing')
    for i,row in enumerate(rows):
        if row.get('kind') not in ('skill_accepted','harness_skill_first_publication'):continue
        when=row.get('monotonic_ns')
        if i<=index or type(when) is not int or when<=stamp:
            raise HarnessError('program_trial_must_precede_actions')
        if row.get('kind')=='skill_accepted' and any(row.get('intent',{}).get(k)!=scope[k]
                                                   for k in TRIAL_SCOPE):
            raise HarnessError('program_trial_action_scope_changed')
    identities=[r for r in rows if r.get('kind')=='run_identity']
    if len(identities)!=1 or identities[0].get('synthetic') is not trial['synthetic']:
        raise HarnessError('program_trial_owner_mode_mismatch')
    return trial


def program_identity(rows, original):
    trial=owner_program_trial(rows)
    if trial is None:return original
    return dict(original,scenario_id=trial['scenario_id'],
        scene_manifest_sha256=trial['manifest_sha256'],heldout=trial['heldout'],
        program_trial=trial,validation_scope=trial['validation_scope'],
        scene_content_sha256=trial['scene_content_sha256'],generalization_proven=False)
