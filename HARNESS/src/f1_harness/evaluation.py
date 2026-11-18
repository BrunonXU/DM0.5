"""Frozen 20-scene acceptance accounting. Synthetic runs cannot satisfy physical acceptance."""
import json
from pathlib import Path
from .contracts import digest


def evaluate(manifest,runs):
    scenarios=manifest.get('scenarios',[])
    ids=[s['scenario_id'] for s in scenarios]
    if len(ids)!=20 or len(set(ids))!=20:raise ValueError('exactly_20_distinct_frozen_scenarios_required')
    if manifest.get('manifest_id')!=digest({k:v for k,v in manifest.items() if k!='manifest_id'}):
        raise ValueError('frozen_manifest_integrity_failure')
    rows=[];seen=set()
    for run in runs:
        root=Path(run)
        sealed=(root/'manifest.json').is_file()
        audit=None
        if sealed:
            from .reporting import summarize_run
            audit=summarize_run(root)  # Verifies every archived file before reading the verdict.
            if audit.get('schema')!='f1-harness-evidence-audit-v1':
                raise ValueError('physical_acceptance_requires_sealed_evidence_pack')
        trace=root/'owner/harness/trace.jsonl' if sealed else root/'trace.jsonl'
        events=[json.loads(line) for line in trace.read_text().splitlines()]
        identity=next((r for r in events if r['kind']=='run_identity'),{})
        scenario=identity.get('scenario_id')
        if scenario not in ids or scenario in seen:raise ValueError('unknown_or_duplicate_test_scene')
        seen.add(scenario)
        results=[r for r in events if r['kind']=='skill_result']
        placement=[r for r in results if r['skill']=='verify_placed']
        assisted=any(r['result']['assisted'] for r in results) or any(r['kind']=='manual_intervention' for r in events)
        verdict=placement[-1]['result'] if placement else {}
        proof=any(r['kind']=='owner_placement_verified' and r.get('request_id')==verdict.get('request_id') for r in events)
        bound=(identity.get('heldout') is True and identity.get('scene_manifest_sha256')==manifest['manifest_id'])
        evidence_complete=bool(audit and audit.get('integrity_complete'))
        assisted=assisted or bool(audit and audit.get('assisted'))
        autonomous=(bound and evidence_complete and audit.get('autonomous_success') is True
                    and identity.get('synthetic') is False and proof and not assisted
                    and verdict.get('outcome')=='success' and verdict.get('synthetic') is False)
        rows.append(dict(scenario_id=scenario,autonomous_success=bool(autonomous),assisted=assisted,
                         pending_review=not autonomous and verdict.get('outcome')!='failure',
                         synthetic=identity.get('synthetic'),run=str(run),
                         sealed_evidence_complete=evidence_complete,frozen_scene_bound=bound))
    successes=sum(r['autonomous_success'] for r in rows)
    false_success=any(manifest.get('known_false_positive_runs',[]))
    return dict(accepted=len(seen)==20 and successes>=18 and not false_success,
                autonomous_successes=successes,total_required=20,completed=len(seen),rows=rows,
                missing=sorted(set(ids)-seen),known_false_positive=bool(false_success),
                scope='single square block and registered matching hole',
                experience_validation_required_separately=True)
