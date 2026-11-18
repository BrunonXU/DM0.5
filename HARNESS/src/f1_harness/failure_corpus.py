"""Content-addressed observed failures, never executable recovery instructions."""
import json
from pathlib import Path
from .contracts import digest
from .evidence import EvidenceStore


def valid_admission_diagnostic(row):
    from .admission_diagnostics import REASONS
    state=row.get('state')
    return (row.get('schema')=='f1-admission-rejection-v1'
        and row.get('reason') in REASONS and row.get('operation') in ('prepare','submit')
        and row.get('no_new_motion') is True and row.get('new_request_queued') is False
        and type(row.get('request_had_publication')) is bool
        and isinstance(state,dict) and type(state.get('needs_correction')) is bool)


def record_failures(run, root):
    run = Path(run).resolve()
    source = run / 'collected/owner/harness/trace.jsonl'
    if not source.is_file():
        return None
    store = EvidenceStore(Path(root) / 'failures')
    source_hash = store.put_blob(source.read_bytes())
    rows = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    skills={r['intent']['request_id']:r['intent']['skill'] for r in rows if r.get('kind')=='skill_accepted'}
    incidents = {}
    # The optional journal has its own queue: append order is not event order.
    # Keep historical aggregation order unchanged, but derive new context only
    # from complete, valid timestamps in this single owner trace.
    ordered=all(type(r.get('monotonic_ns')) is int and r['monotonic_ns']>=0 for r in rows)
    prior_by_index={};prior=None;prior_ns=None
    if ordered:
        for index,row in sorted(enumerate(rows),key=lambda item:item[1]['monotonic_ns']):
            if row.get('kind')=='task_prepared':prior=None;prior_ns=None
            prior_by_index[index]=prior if prior_ns is not None and prior_ns<row['monotonic_ns'] else None
            if row.get('kind')=='skill_result':
                result=row.get('result',{})
                prior=dict(skill=row.get('skill'),request_id=result.get('request_id'),
                    outcome=result.get('outcome'),event_id=row.get('event_id'))
                prior_ns=row['monotonic_ns']
    # Prefer the richer optional diagnostic when the legacy rejection receipt
    # also exists. A dropped optional diagnostic still leaves the old receipt.
    detailed={(r.get('request_id'),r.get('reason')) for r in rows
              if r.get('kind')=='skill_admission_rejected' and r.get('operation')=='submit'
              and valid_admission_diagnostic(r)}
    for index,row in enumerate(rows):
        kind = row.get('kind')
        preceding_result=prior_by_index.get(index)
        if kind == 'skill_result':
            result = row.get('result', {})
            if result.get('outcome') == 'success':
                continue
            fact = dict(skill=row.get('skill'), reason=result.get('reason'),
                        outcome=result.get('outcome'), request_id=result.get('request_id'))
        elif kind in ('harness_verification_wait', 'harness_verification_escalated',
                      'harness_motion_checkpoint'):
            fact = dict(skill=row.get('skill'), reason=row.get('reason') or kind,
                        outcome='waiting' if kind.endswith('_wait') else 'diagnostic',
                        request_id=row.get('request_id'), observation_reason=row.get('observation_reason'))
        elif kind=='skill_admission_rejected':
            state=row.get('state')
            valid=valid_admission_diagnostic(row)
            fact=dict(skill=row.get('skill'),request_id=row.get('request_id'),outcome='rejected',
                reason=row.get('reason') if valid else 'invalid_admission_diagnostic',
                operation=row.get('operation'),new_motion_inferred=False,
                state=state if valid else None,preceding_result=preceding_result,
                request_had_publication=row.get('request_had_publication') if valid else None,
                task_hash=row.get('task_hash'),epoch=row.get('epoch'),
                causal_status='timestamp_order_only_not_physical_cause' if ordered else 'ordering_unavailable')
        elif kind=='skill_not_admitted':
            if (row.get('request_id'),row.get('reason')) in detailed:continue
            fact=dict(skill=skills.get(row.get('request_id')),reason=row.get('reason'),
                request_id=row.get('request_id'),outcome='rejected',new_motion_inferred=False,
                preceding_result=preceding_result,
                causal_status='timestamp_order_only_not_physical_cause' if ordered else 'ordering_unavailable')
        else:
            continue
        if not fact.get('skill'):fact['skill']=skills.get(fact.get('request_id'))
        key = digest(fact)
        entry = incidents.setdefault(key, dict(**fact, kind=kind, count=0,
                    first_ns=row.get('monotonic_ns'), last_ns=row.get('monotonic_ns')))
        entry['count'] += 1
        entry['last_ns'] = row.get('monotonic_ns')
    value = dict(schema='f1-failure-facts-v1', run=str(run), source_sha256=source_hash,
                 incidents=list(incidents.values()), executable=False,
                 cause_status='reported_codes_only_not_physical_root_cause')
    identity = digest(value)
    path = store.root / (identity + '.json')
    if not path.exists():
        store.write_json(path.name, value)
    return dict(id=identity, path=str(path), incident_count=len(incidents))
