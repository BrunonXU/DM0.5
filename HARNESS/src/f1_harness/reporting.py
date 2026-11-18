"""Summarize existing evidence without turning planner declarations into measured success."""
from pathlib import Path
import json


def summarize_run(run):
    root=Path(run);trace=root/'trace.jsonl'
    if (root/'manifest.json').is_file():
        manifest=json.loads((root/'manifest.json').read_text())
        if manifest.get('schema')=='f1-harness-evidence-pack-v1':
            from .evidence_pack import file_hash
            for name,value in manifest['files'].items():
                path=(root/name).resolve()
                if root.resolve() not in path.parents or file_hash(path)!=value['sha256']:
                    raise ValueError('sealed_evidence_hash_mismatch')
            return dict(run=str(root.resolve()),**json.loads((root/'audit.json').read_text()))
    rows=[json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    prepared=[r for r in rows if r.get('kind')=='remote_task_prepared']
    if prepared:
        from .evidence_pack import _scope
        imported=[r['record'] for r in rows if r.get('kind')=='owner_event']
        try:scoped=_scope(imported,prepared[-1])
        except (ValueError,KeyError):scoped=[]
        rows=[r for r in rows if r.get('kind')!='owner_event']+scoped
    rows=[r['record'] if r['kind']=='owner_event' else r for r in rows]
    outcomes=[r for r in rows if r['kind']=='skill_result']
    accepted={r['intent']['request_id']:r for r in rows if r['kind']=='skill_accepted'}
    models=[r for r in rows if r['kind']=='model_response']
    result=json.loads((root/'result.json').read_text()) if (root/'result.json').exists() else None
    failure=json.loads((root/'failure.json').read_text()) if (root/'failure.json').exists() else None
    assisted=any(r['kind']=='manual_intervention' for r in rows) or any(r['result'].get('assisted') for r in outcomes)
    assisted=assisted or any(r['kind']=='authority_revoked' and r.get('reason')=='manual_takeover' for r in rows)
    measured=(result or {}).get('measured',{})
    steps=[]
    for row in outcomes:
        value=row['result'];start=accepted.get(value['request_id'],{}).get('monotonic_ns')
        steps.append(dict(skill=row['skill'],request_id=value['request_id'],outcome=value['outcome'],reason=value['reason'],
            seconds=None if start is None else (row['monotonic_ns']-start)/1e9,assisted=value.get('assisted',False)))
    return dict(run=str(root.resolve()),measured=measured or None,steps=steps,
                physical_success_claimed=False if not measured else measured.get('outcome')=='success' and measured.get('synthetic') is False,
                physical_success_verified=False,autonomous_success=False,
                evidence_status='awaiting_native_recording_seal' if prepared else 'unsealed_or_synthetic',
                assisted=bool(assisted or measured.get('assisted')),failure=failure,
                llm_calls=len(models),llm_seconds=sum(r['elapsed_s'] for r in models),
                note='Planner declarations are not success evidence. Remote owner trace is required when steps are absent.')
