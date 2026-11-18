"""Post-run tracking facts; never motion admission, force or recovery advice.

Command publication and encoder observations are separate evidence streams.
Repeated control-loop rows do not establish fresh/stationary encoder feedback.
Reports are immutable sidecars; historical failure/program identities stay intact.
"""
import hashlib
import json
from pathlib import Path
import numpy as np
from .contracts import digest
from .evidence import EvidenceStore


def _vector(value, size):
    try:
        q = np.asarray(value, float)
        return q if q.shape == (size,) and np.isfinite(q).all() else None
    except (TypeError, ValueError):
        return None


def tracking_diagnostics(trace, controls, observations):
    """Describe the last second preceding each owner checkpoint in owner time."""
    results = []
    for ordinal, checkpoint in enumerate(trace):
        if checkpoint.get('kind') != 'harness_motion_checkpoint':
            continue
        request, end = checkpoint.get('request_id'), checkpoint.get('monotonic_ns')
        if not request or type(end) is not int:
            continue
        before = trace[:ordinal]
        candidates = [r for r in before if r.get('kind') == 'harness_plan_waiting_fresh_revalidation'
                      and r.get('request_id') == request and type(r.get('monotonic_ns')) is int
                      and r['monotonic_ns'] <= end]
        accepted = [r for r in before if r.get('kind') == 'skill_accepted'
                    and r.get('intent', {}).get('request_id') == request
                    and type(r.get('monotonic_ns')) is int and r['monotonic_ns'] <= end]
        item = dict(request_id=request, checkpoint_ns=end, index=checkpoint.get('index'),
                    scope='last_second_before_owner_checkpoint', physical_cause='unknown',
                    force_estimated=False, control_changed=False)
        if not candidates or not accepted:
            item['status'] = 'request_plan_or_acceptance_missing';results.append(item);continue
        start = accepted[-1]['monotonic_ns']
        intent = accepted[-1]['intent']
        plan = candidates[-1]['candidate']
        index = checkpoint.get('index');targets = plan.get('targets', [])
        goal = _vector(targets[index], 16) if type(index) is int and 0 <= index < len(targets) else None
        if goal is None or not start <= end:
            item['status'] = 'invalid_checkpoint_or_plan';results.append(item);continue
        if (not all(key in intent and checkpoint.get(key) == intent[key] for key in ('epoch', 'task_hash'))
                or plan.get('epoch') != intent['epoch']
                or plan.get('request_id') != request
                or candidates[-1]['monotonic_ns'] < start):
            item['status'] = 'checkpoint_plan_scope_mismatch';results.append(item);continue
        item['skill'] = accepted[-1]['intent'].get('skill')
        window_start = max(start, end-1_000_000_000)
        request_rows = [r for r in controls if type(r.get('monotonic_ns')) is int
                        and start <= r['monotonic_ns'] <= end
                        and r.get('source') == 'harness_skill'
                        and all((r.get('policy_sampling') or {}).get(k) == intent[k]
                                for k in ('request_id', 'epoch', 'task_hash'))]
        request_rows.sort(key=lambda r: r['monotonic_ns'])
        published = [r for r in request_rows if r.get('publication_complete') is True
                     and any(p.get('topic_key') == 'joint_command' for p in r.get('publications', []))
                     and _vector(r.get('submitted'), 16) is not None]
        item['incomplete_publication_rows'] = sum(r.get('publication_complete') is not True for r in request_rows)
        if published:
            last = published[-1];submitted = np.asarray(last['submitted'])
            item.update(last_joint_publication_row_ns=last['monotonic_ns'],
                target_minus_last_submitted_deg=(goal[7:14]-submitted[7:14]).tolist(),
                last_limit_control=last.get('limit_control'))
        else:
            item['status'] = 'joint_publication_missing';results.append(item);continue
        # Count complete gripper-topic publications, not an intended closure in a
        # planned waypoint or a carried-forward value in a joint-only record.
        grip = [r for r in request_rows if r.get('publication_complete') is True
                and any(p.get('topic_key') == 'right_gripper_command' for p in r.get('publications', []))
                and _vector(r.get('submitted'), 16) is not None]
        item['published_gripper_values'] = sorted({float(r['submitted'][15]) for r in grip})
        valid = {};conflicting = set();invalid = 0
        for obs in observations:
            stamps = obs.get('state_timestamps_ns') or []
            mask = obs.get('state_valid_mask') or []
            measured = _vector(obs.get('state16'), 16)
            if len(stamps) != 16 or type(stamps[7]) is not int or not window_start <= stamps[7] <= end:
                continue
            if (len(mask) != 16 or not all(v is True for v in mask[7:14]) or measured is None
                    or len(set(stamps[7:14])) != 1):
                invalid += 1;continue
            stamp = stamps[7];q = measured[7:14]
            if stamp in valid and not np.array_equal(valid[stamp], q):
                conflicting.add(stamp)
            valid[stamp] = q
        for stamp in conflicting:valid.pop(stamp)
        item.update(unique_right_encoder_samples=len(valid),
                    invalid_encoder_rows=invalid, conflicting_encoder_timestamps=len(conflicting))
        if len(valid) < 2 or conflicting:
            item['status'] = 'insufficient_or_conflicting_encoder_evidence'
        else:
            ordered = sorted(valid);last_q = valid[ordered[-1]]
            item.update(status='measured_tracking_facts',
                encoder_span_s=(ordered[-1]-ordered[0])/1e9,
                last_encoder_age_at_checkpoint_ms=(end-ordered[-1])/1e6,
                measured_right_range_deg=np.ptp([valid[t] for t in ordered], axis=0).tolist(),
                target_minus_last_encoder_deg=(goal[7:14]-last_q).tolist(),
                last_submission_precedes_last_encoder=last['monotonic_ns'] <= ordered[-1])
        results.append(item)
    return dict(schema='f1-tracking-diagnostics-v1', checkpoints=results, executable=False,
        limitations=['Joint tracking error is not object-relative capture error or contact force.',
                     'No success, physical cause, replay permission or new tolerance inferred.',
                     'No stationarity claim from repeated control rows; encoder timestamps are distinct.',
                     'A complete publication records sending, not actuator reception or physical completion.'])


def record_tracking_diagnostics(run, root):
    run = Path(run).resolve()
    owner = run / 'collected/owner'
    paths = [owner/'harness/trace.jsonl', owner/'journal/control.jsonl', owner/'journal/observations.jsonl']
    if not paths[0].is_file():return None
    contents = [p.read_bytes() if p.is_file() else None for p in paths]
    rows = [[json.loads(line) for line in data.splitlines() if line.strip()] if data is not None else []
            for data in contents]
    value = tracking_diagnostics(*rows)
    value.update(run=str(run), sources={str(p.relative_to(run)):hashlib.sha256(data).hexdigest()
                 if data is not None else None for p,data in zip(paths,contents)})
    store = EvidenceStore(Path(root)/'tracking')
    key = digest(value);path = store.root/(key+'.json')
    if not path.exists():store.write_json(path.name,value)
    return dict(id=key,path=str(path),checkpoint_count=len(value['checkpoints']),executable=False)
