"""Seal copied native recording and planner evidence without granting training or motion authority.

All large I/O and optional rendering happen after the owner has stopped. Source
recordings remain immutable. A complete file package can still describe a failed
or unverified task; integrity and physical outcome are reported separately.
"""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import os

from .contracts import digest


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _copy_file(source, target):
    source=Path(source)
    if source.is_symlink() or any(p.is_symlink() for p in source.parents):
        raise ValueError('evidence_source_symlink_forbidden')
    if not source.is_file():raise ValueError('evidence_file_missing:'+source.name)
    before=file_hash(source)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)
    if file_hash(target)!=before or file_hash(source)!=before:
        raise ValueError('evidence_changed_during_copy')


def _scope(rows, prepared):
    starts=[i for i,r in enumerate(rows) if r.get('kind')=='task_prepared'
            and r.get('task_hash')==digest(prepared['task'])
            and r.get('session_id')==prepared['session_id']
            and r.get('owner_instance_id')==prepared['owner_instance_id']]
    if len(starts)!=1:raise ValueError('unique_bound_owner_task_not_found')
    begin=starts[0]
    end=next((i for i in range(begin+1,len(rows)) if rows[i].get('kind')=='task_prepared'),len(rows))
    # Keep subsequent manual reset/shutdown in the raw archive, outside this task's verdict.
    closed=next((i for i in range(begin+1,end)
                 if rows[i].get('kind')=='authority_revoked'
                 and rows[i].get('reason') in ('task_finished','session_closed','owner_bridge_closed')),None)
    if closed is not None:end=closed+1
    return rows[begin:end]


def _native_audit(owner_run):
    try:
        from f1dagger.recording import audit
    except ImportError:
        # The 6000D task service deliberately has no robot package imports.
        # Reuse the registered sibling project's read-only auditor in its own
        # process rather than mutating sys.path in a multithreaded supervisor.
        root=Path(__file__).resolve().parents[3]
        paths=[root/'DAgger/F1/src',root/'DAgger/shared/src']
        if not (paths[0]/'f1dagger/recording.py').is_file() or not (paths[1]/'dagger_core').is_dir():
            raise RuntimeError('seal_requires_existing_DAgger_F1_and_shared_on_PYTHONPATH') from None
        from .site import _safe_environment
        env=_safe_environment();env['PYTHONPATH']=os.pathsep.join(map(str,paths))
        result=subprocess.run([sys.executable,'-c',
            'import json,sys; from pathlib import Path; from f1dagger.recording import audit; '
            'print(json.dumps(audit(Path(sys.argv[1]))))',str(Path(owner_run).resolve())],
            env=env,capture_output=True,text=True,timeout=120,check=True)
        return json.loads(result.stdout)
    return audit(owner_run)


def inspect_pair(planner_run, owner_run):
    """Audit hashes, task identity, original event IDs and real publication references."""
    p,o=Path(planner_run),Path(owner_run)
    issues=[]
    raw=_native_audit(o)
    if not raw['valid']:issues.extend('native_raw:'+x for x in raw['errors'])
    planner=read_rows(p/'trace.jsonl')
    prepared=[r for r in planner if r.get('kind')=='remote_task_prepared']
    if len(prepared)!=1:raise ValueError('one_prepared_task_required')
    prepared=prepared[0]
    identity=prepared.get('recording_identity') or {}
    native=json.loads((o/'journal/manifest.json').read_text())
    binding=native.get('binding',{})
    if (binding.get('purpose')!='harness_evaluation' or binding.get('training_ready') is not False
            or binding.get('collection_purpose')!='evaluation'):
        issues.append('native_recording_not_harness_evaluation')
    if binding.get('execution_mode')!='powered':issues.append('native_recording_not_powered')
    if binding.get('synthetic') is True:issues.append('native_recording_is_synthetic')
    if identity.get('owner_run_id')!=binding.get('owner_run_id') or not identity.get('owner_run_id'):
        issues.append('owner_run_identity_mismatch')
    if binding.get('harness_owner_instance_id')!=prepared.get('owner_instance_id'):
        issues.append('owner_instance_mismatch')
    if not identity.get('host_boot_id') or identity.get('host_boot_id')!=binding.get('host_boot_id'):
        issues.append('owner_boot_identity_mismatch')
    source_rows=read_rows(o/'harness/trace.jsonl')
    scoped=_scope(source_rows,prepared)
    original={r['event_id']:r for r in scoped}
    source_hashes={json.loads(line).get('event_id'):hashlib.sha256(line.encode()).hexdigest()
                   for line in (o/'harness/trace.jsonl').read_text().splitlines(keepends=True)}
    if len(original)!=len(scoped):issues.append('duplicate_owner_event_id')
    imported=[r['record'] for r in planner if r.get('kind')=='owner_event']
    imported_ids=set()
    for row in imported:
        if row.get('event_id') not in original:continue  # Other tasks in the same owner journal.
        if row.get('kind')=='owner_event_reference':
            if (row.get('artifact')!='harness/trace.jsonl'
                    or row.get('sha256')!=source_hashes.get(row['event_id'])):
                issues.append('owner_export_content_mismatch')
        elif original[row['event_id']]!=row:issues.append('owner_export_content_mismatch')
        imported_ids.add(row['event_id'])
    if not imported_ids:issues.append('owner_trace_export_missing')
    if any(r.get('kind') in ('owner_evidence_export_incomplete','image_evidence_incomplete') for r in planner):
        issues.append('planner_evidence_export_incomplete')
    accepted={r['intent']['request_id']:r['intent'] for r in scoped if r.get('kind')=='skill_accepted'}
    authorizations={r['epoch'] for r in scoped if
                    (r.get('kind')=='local_authorization' or r.get('kind')=='web_authorization'
                     and r.get('web_request_id') and r.get('owner_run_id')==identity.get('owner_run_id'))
                    and r.get('synthetic') is False and r.get('session_id')==prepared['session_id']
                    and r.get('task_hash')==digest(prepared['task'])
                    and r.get('owner_instance_id')==prepared['owner_instance_id']}
    if not authorizations:issues.append('physical_authorization_evidence_missing')
    for intent in accepted.values():
        if (intent['task_hash']!=digest(prepared['task']) or intent['session_id']!=prepared['session_id']
                or intent.get('calibration_id')!=prepared['task']['calibration_id']
                or intent['owner_instance_id']!=prepared['owner_instance_id'] or intent['epoch'] not in authorizations):
            issues.append('accepted_skill_scope_mismatch')
    control=read_rows(o/'journal/control.jsonl')
    publications={str(r['command_sequence']):r for r in control
                  if r.get('publication_complete') is True and r.get('submitted') is not None
                  and r.get('execution_mode')=='powered'}
    waypoints=[r for r in scoped if r.get('kind')=='harness_measured_waypoint']
    for row in waypoints:
        pub=publications.get(str(row['publication_id']))
        if pub is None:issues.append('waypoint_publication_missing');continue
        sampling=pub.get('policy_sampling') or {}
        if sampling.get('request_id')!=row['request_id'] or sampling.get('task_hash')!=digest(prepared['task']):
            issues.append('publication_task_binding_mismatch')
    # A faithfully recorded failure before any motion is still auditable.
    # Successful motion claims are required to have waypoints below.
    final=[r for r in scoped if r.get('kind')=='skill_result' and r.get('skill')=='verify_placed']
    verified=bool(final and final[-1]['result'].get('outcome')=='success'
                  and final[-1]['result'].get('synthetic') is False
                  and final[-1]['result']['request_id'] in accepted)
    required={'approach_grasp','grasp','verify_grasp','inspect_in_hand','transfer_above_hole','align','insert','release','verify_placed'}
    reset_required='withdraw_reset' in prepared['task']['allowed_skills']
    if reset_required:required.add('withdraw_reset')
    completed={r['skill'] for r in scoped if r.get('kind')=='skill_result'
               and r['result'].get('outcome')=='success' and r['result'].get('synthetic') is False}
    if verified and not required-{'withdraw_reset'}<=completed:issues.append('complete_task_skill_evidence_missing')
    reset_verified='withdraw_reset' in completed
    waypoint_requests={r['request_id'] for r in waypoints}
    for row in scoped:
        if row.get('kind')!='skill_result':continue
        if row['result']['request_id'] not in accepted:issues.append('result_without_accepted_skill')
        elif accepted[row['result']['request_id']]['skill']!=row['skill']:issues.append('result_skill_identity_mismatch')
        motion_request=motion_evidence_request(row['result']['request_id'],accepted,scoped)
        if (row['skill'] in required-{'verify_grasp','inspect_in_hand','verify_placed'}
                and row['result'].get('outcome')=='success' and motion_request not in waypoint_requests):
            issues.append('motion_result_without_measured_waypoint')
        for sha in row['result'].get('evidence_ids',[]):
            if not re.fullmatch('[a-f0-9]{64}',sha):issues.append('invalid_verifier_evidence_digest');continue
            path=p/'blobs'/sha
            if not path.is_file() or file_hash(path)!=sha:issues.append('verifier_image_evidence_missing')
    if verified and not final[-1]['result'].get('evidence_ids'):issues.append('placement_image_evidence_missing')
    assisted=any(r.get('kind')=='manual_intervention' or
                 (r.get('kind')=='authority_revoked' and r.get('reason')=='manual_takeover') or
                 (r.get('kind')=='skill_result' and r['result'].get('assisted')) for r in scoped)
    declared=json.loads((p/'result.json').read_text()).get('measured',{}) if (p/'result.json').is_file() else {}
    assisted=assisted or bool(declared.get('assisted'))
    if not (declared.get('synthetic') is False and (declared.get('placement_success') is True
            or not reset_required and declared.get('outcome')=='success')):verified=False
    complete=bool(verified and (not reset_required or reset_verified) and declared.get('outcome')=='success')
    issues=sorted(set(issues))
    return dict(schema='f1-harness-evidence-audit-v1',training_ready=False,
        event_mirror_complete=not any(r.get('kind')=='owner_evidence_export_incomplete' for r in planner),
        planner_images_complete=not any(r.get('kind')=='image_evidence_incomplete' for r in planner),
        task=prepared['task'],session_id=prepared['session_id'],owner_instance_id=prepared['owner_instance_id'],
        recording_identity=identity,epochs=sorted(authorizations),native_audit=raw,
        integrity_complete=not issues,issues=issues,verified_placement=verified and not issues,
        reset_required=reset_required,verified_reset=reset_verified and not issues,
        autonomous_success=bool(complete and not issues and not assisted),assisted=assisted,
        task_outcome='success' if complete and not issues else 'pending_review',
        counts=dict(accepted_skills=len(accepted),measured_waypoints=len(waypoints),owner_events=len(scoped)),
        training_note='Evaluation evidence only; not an expert or BC/RLT training admission.')


def motion_evidence_request(request_id,accepted,rows):
    """Resolve a verification-only result to its original owner-proven motion."""
    intent=accepted.get(request_id,{})
    original=intent.get('verification_request_id')
    if not original:return request_id
    parent=accepted.get(original,{})
    if (parent.get('verification_request_id') or not parent
            or any(parent.get(k)!=intent.get(k) for k in
                   ('skill','task_hash','session_id','owner_instance_id','epoch'))):
        return None
    continued=next((r for r in rows if r.get('kind')=='harness_verification_continued'
        and r.get('request_id')==request_id and r.get('motion_request_id')==original
        and r.get('motion_replayed') is False and r.get('skill')==intent.get('skill')
        and r.get('epoch')==intent.get('epoch') and r.get('task_hash')==intent.get('task_hash')),None)
    escalated=next((r for r in rows if r.get('kind')=='harness_verification_escalated'
        and r.get('request_id')==original and continued is not None
        and r.get('after_ns')==continued.get('after_ns') and r.get('motion_replayed') is False),None)
    return original if continued is not None and escalated is not None else None


def seal_run(planner_run, owner_run, output, *, video_root=None, render_video=False):
    """Create a new, self-contained evidence directory. Never rewrites a source run."""
    for supplied in (planner_run,owner_run):
        path=Path(supplied).absolute()
        if any(part.is_symlink() for part in (path,*path.parents)):raise ValueError('evidence_root_symlink_forbidden')
    p,o,out=Path(planner_run).resolve(),Path(owner_run).resolve(),Path(output).absolute()
    if out.exists() or out.with_name(out.name+'.partial').exists():raise FileExistsError(out)
    if p in out.parents or o in out.parents:raise ValueError('seal_output_must_be_outside_source')
    stage=out.with_name(out.name+'.partial');stage.mkdir(parents=True)
    files={}
    def copy(source,relative):
        _copy_file(source,stage/relative)
        files[relative]=dict(sha256=file_hash(stage/relative),bytes=(stage/relative).stat().st_size)
    # Explicit names only: API files, worker environments and arbitrary configs never enter the bundle.
    for name in ('trace.jsonl','result.json','failure.json'):
        if (p/name).is_file():copy(p/name,'planner/'+name)
    for blob in (p/'blobs').iterdir() if (p/'blobs').is_dir() else ():
        if not re.fullmatch('[a-f0-9]{64}',blob.name):raise ValueError('invalid_blob_filename')
        if file_hash(blob)!=blob.name:raise ValueError('planner_blob_hash_mismatch')
        copy(blob,'planner/blobs/'+blob.name)
    journal_names=('manifest.json','observations.jsonl','policy_chunks.jsonl','control.jsonl','events.jsonl')
    payload_names=('manifest.json','frames.jsonl','head.pack','left_wrist.pack','right_wrist.pack')
    for folder,names in {'journal':journal_names,'journal.partial':journal_names,
                         'payload':payload_names,'payload.partial':payload_names,
                         'harness':('trace.jsonl',)}.items():
        for name in names:
            if (o/folder/name).is_file():copy(o/folder/name,'owner/'+folder+'/'+name)
    for name in ('failure.txt','camera_diagnostics.json'):
        if (o/name).is_file():copy(o/name,'owner/'+name)
    if (o/'scene/manifest.json').is_file():
        copy(o/'scene/manifest.json','owner/scene/manifest.json')
        for blob in (o/'scene/blobs').iterdir() if (o/'scene/blobs').is_dir() else ():
            if not re.fullmatch('[a-f0-9]{64}',blob.name) or file_hash(blob)!=blob.name:
                raise ValueError('scene_blob_identity_mismatch')
            copy(blob,'owner/scene/blobs/'+blob.name)
    # Audit the frozen copies, so a still-running source cannot race the final interpretation.
    try:report=inspect_pair(stage/'planner',stage/'owner')
    except (ValueError,KeyError,TypeError,OSError) as exc:
        report=dict(schema='f1-harness-evidence-audit-v1',integrity_complete=False,
                    autonomous_success=False,verified_placement=False,task_outcome='pending_review',
                    training_ready=False,issues=[type(exc).__name__+':'+str(exc)])
    if 'native_audit' in report:report['native_audit']['run']='owner'
    report['raw_recording_complete']=bool((report.get('native_audit') or {}).get('valid'))
    report.setdefault('event_mirror_complete',False)
    if (stage/'owner/scene/manifest.json').is_file():
        report['scene_archive']=json.loads((stage/'owner/scene/manifest.json').read_text())
    if render_video and report['raw_recording_complete']:
        renderer=Path(__file__).resolve().parents[3]/'F1-4242/scripts/session_preview_worker.py'
        spec=importlib.util.spec_from_file_location('f1_existing_session_preview',renderer)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        try:
            module.render(stage/'owner',stage/'video',file_hash(stage/'owner/journal/manifest.json'))
        except Exception as exc:
            # Rendering is a derived artifact. Preserve the audited originals
            # and report its failure independently of execution and recording.
            report['video_error']=dict(type=type(exc).__name__,reason=str(exc))
        report['renderer_source_sha256']=file_hash(renderer)
    elif video_root is not None:
        for name in ('head.mp4','left.mp4','right.mp4','timeline.json','analytics.json'):
            copy(Path(video_root)/name,'video/'+name)
    if (stage/'video/timeline.json').is_file() and not report.get('video_error'):
        timeline=json.loads((stage/'video/timeline.json').read_text())
        if timeline.get('source_sha256')!=file_hash(stage/'owner/journal/manifest.json'):
            raise ValueError('video_source_identity_mismatch')
        report['video_status']='derived_review_only'
        for f in (stage/'video').iterdir():
            if f.is_file():files[str(f.relative_to(stage))]=dict(sha256=file_hash(f),bytes=f.stat().st_size)
    else:
        report['video_status']='render_failed_original_frames_preserved' if report.get('video_error') else 'not_rendered_original_frames_preserved'
        for f in (stage/'video').iterdir() if (stage/'video').is_dir() else ():
            if f.is_file():files[str(f.relative_to(stage))]=dict(sha256=file_hash(f),bytes=f.stat().st_size)
    (stage/'audit.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    files['audit.json']=dict(sha256=file_hash(stage/'audit.json'),bytes=(stage/'audit.json').stat().st_size)
    manifest=dict(schema='f1-harness-evidence-pack-v1',training_ready=False,
                  sources=dict(planner=str(p),owner=str(o)),files=files,
                  integrity_complete=report['integrity_complete'],autonomous_success=report['autonomous_success'])
    (stage/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    stage.rename(out)
    return dict(output=str(out),manifest_sha256=file_hash(out/'manifest.json'),**report)
