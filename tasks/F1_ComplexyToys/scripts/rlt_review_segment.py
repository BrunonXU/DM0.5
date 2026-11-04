#!/usr/bin/env python3
"""Publish a source-bound, versioned local outcome/expert review; raw stays read-only.

The decision JSON is explicit human review, never inferred from editable notes.
Use --inspect first to obtain exact observation and human-interval identities.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_local_replay import read_prefix, OBJECTIVE, HUMAN


def prepare(run, decision):
    raw = (run/'local_segments/placement-1.json').read_bytes()
    receipt = json.loads(raw)
    streams = read_prefix(run, receipt)
    result = receipt['local_result']
    if receipt['binding'].get('collection_purpose') == 'evaluation':
        raise ValueError('Evaluation is isolated; use its task result review')
    if decision.get('environment_assistance_ranges_ns'):
        raise ValueError('External object reset needs a separately split trajectory review; do not approve an undivided local reward')
    if not str(decision.get('reviewer', '')).strip() or not str(decision.get('reason', '')).strip():
        raise ValueError('Reviewer and evidence/reason required')
    status = decision.get('status')
    if status not in ('success', 'failure', 'pending_review'):
        raise ValueError('Explicit success/failure/pending_review required')
    if status == 'success' and decision.get('placement_release_confirmed') is not True:
        raise ValueError('Success needs placement and release confirmation')
    if status == 'failure' and decision.get('failure_confirmed') is not True:
        raise ValueError('Failure needs explicit confirmation; uncertainty is pending_review')
    observations = {r['observation_id']:r for r in streams['observations']}
    def endpoint(key):
        r = observations[decision[key]]
        t = r['available_monotonic_ns']
        if not result['started_ns'] < t <= result['ended_ns']:
            raise ValueError('Endpoint outside original local task; no VLA/Home relabel')
        return dict(observation_id=r['observation_id'], available_monotonic_ns=t)
    review = dict(schema='f1_local_review_v2', episode_id=receipt['episode_id'],
        segment_id=result['segment_id'], source_receipt_sha256=hashlib.sha256(raw).hexdigest(),
        objective=OBJECTIVE, approved=True, reviewer=decision['reviewer'], reason=decision['reason'],
        status=status, expert_intervals=decision.get('expert_intervals', []))
    if status in ('success', 'failure'):
        review.update(terminal_observation=endpoint('terminal_observation_id'),
                      placement_release_confirmed=status=='success', failure_confirmed=status=='failure')
    elif 'verified_nonterminal_observation_id' in decision:
        if decision.get('nonterminal_confirmed') is not True:
            raise ValueError('Prefix admission requires explicitly verified nonterminal state')
        review.update(verified_nonterminal_until=endpoint('verified_nonterminal_observation_id'),
                      nonterminal_confirmed=True)
    # Approve whole intervals, but ensure every included published tick is human,
    # local and before the reviewed terminal. No success -> expert inference.
    end = review.get('terminal_observation', {}).get('available_monotonic_ns', result['ended_ns'])
    previous = result['started_ns']
    for span in review['expert_intervals']:
        if set(span) != {'start_ns', 'end_ns'} or any(type(v) is not int for v in span.values()):
            raise ValueError('Expert ranges require integer start_ns/end_ns')
        a,b = span['start_ns'],span['end_ns']
        if not previous <= a < b <= end:
            raise ValueError('Expert ranges must be sorted, disjoint and inside local task')
        ticks = [r for r in streams['control'] if a <= r['monotonic_ns'] <= b]
        if not ticks or any(r.get('source') not in HUMAN or r.get('publication_complete') is not True
                            or r.get('submitted') is None for r in ticks):
            raise ValueError('Expert interval includes nonhuman or unexecuted action')
        previous = b
    return review


def publish(run, decision, reviews_root):
    import fcntl
    review = prepare(run, decision)
    reviews_root.mkdir(parents=True, exist_ok=True)
    with (reviews_root/'.review.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = reviews_root/(review['episode_id']+'.json')
        history = reviews_root/'history'/review['episode_id']; history.mkdir(parents=True, exist_ok=True)
        if target.exists():
            old = target.read_bytes(); old_id = hashlib.sha256(old).hexdigest()
            archived = history/(old_id+'.json')
            if not archived.exists(): archived.write_bytes(old)
            review['supersedes_sha256'] = old_id
        review['reviewed_unix_ns'] = time.time_ns()
        data = (json.dumps(review, ensure_ascii=False, indent=2)+'\n').encode()
        revision = hashlib.sha256(data).hexdigest()
        immutable = history/(revision+'.json'); immutable.write_bytes(data)
        temporary = target.with_suffix('.new'); temporary.write_bytes(data); os.replace(temporary,target)
    return dict(path=str(target), revision=revision, review=review)


def sft_review(run, local_review, *, source_group, split):
    """Use the SAME explicit expert ranges for the existing independent SFT consumer."""
    if not source_group or split not in ('train','validation'):
        raise ValueError('Prospective source group and split required')
    receipt_raw=(run/'local_segments/placement-1.json').read_bytes()
    if hashlib.sha256(receipt_raw).hexdigest()!=local_review['source_receipt_sha256']:
        raise ValueError('Local review source changed')
    manifest_raw=(run/'journal/manifest.json').read_bytes()
    manifest=json.loads(manifest_raw)
    if manifest.get('binding',{}).get('collection_purpose')=='evaluation':
        raise ValueError('Evaluation cannot become SFT')
    ranges=local_review['expert_intervals']
    if not ranges:raise ValueError('No approved expert intervals; no SFT export')
    streams=read_prefix(run,json.loads(receipt_raw))
    interventions=sorted({r['intervention_id'] for r in streams['control']
        if r.get('intervention_id') is not None and r.get('source') in HUMAN
        and any(s['start_ns']<=r['monotonic_ns']<=s['end_ns'] for s in ranges)})
    return dict(local_review,contract='f1_upper_16d_v1',
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),human_review_accepted=True,
        accepted_interventions=interventions,
        accepted_time_ranges_ns=[[s['start_ns'],s['end_ns']] for s in ranges],
        dataset_role='correction',source_group=source_group,split=split,
        environment_assistance_ranges_ns=[])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--decision', type=Path)
    p.add_argument('--reviews-root', type=Path)
    p.add_argument('--inspect', action='store_true')
    p.add_argument('--sft-review-output',type=Path,help='Optional new SFT review file; requires sealed raw manifest and approved expert intervals')
    args=p.parse_args()
    if args.inspect:
        receipt=json.loads((args.run/'local_segments/placement-1.json').read_text())
        streams=read_prefix(args.run,receipt);r=receipt['local_result']
        print(json.dumps(dict(local_result=r, observations=[dict(observation_id=o['observation_id'],
            available_monotonic_ns=o['available_monotonic_ns']) for o in streams['observations']
            if r['started_ns']<=o['available_monotonic_ns']<=r['ended_ns']]),indent=2))
    else:
        if not args.decision or not args.reviews_root:p.error('--decision and --reviews-root required')
        decision=json.loads(args.decision.read_text())
        result=publish(args.run,decision,args.reviews_root)
        if args.sft_review_output:
            value=sft_review(args.run,result['review'],source_group=decision.get('source_group'),split=decision.get('split'))
            with args.sft_review_output.open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
