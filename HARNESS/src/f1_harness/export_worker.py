"""Durable local export queue; consumes closed robot runs without owning motion."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .collect import collect_rollout
from .evidence import EvidenceStore

JOB_NAME = 'export-job-v2.json'


def pending_job(job):
    """Transfer and review have independent, bounded retry state."""
    return (job.get('state') in ('queued','running') or
        job.get('state')=='completed' and bool(job.get('experience_root'))
        and (job.get('experience_review') or {}).get('status') in ('queued','running'))


def processor_identity():
    """Bind export and review to the coordinator's immutable source release."""
    source = Path(__file__).resolve().parent.parent
    files = ('export_worker.py', 'run_review.py', 'memory.py', 'program_experience.py')
    hashes = {name: hashlib.sha256((source/'f1_harness'/name).read_bytes()).hexdigest()
              for name in files}
    value = dict(source_root=str(source), source_sha256=hashes)
    value['id'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def job_path(run):
    """Keep older completed runs readable without exposing new jobs to old workers."""
    run = Path(run)
    current = run / JOB_NAME
    return current if current.exists() else run / 'export-job.json'


def save(path, value):
    temporary = path.with_suffix('.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def enqueue(run, config, receipt, *, stop_confirmed):
    run = Path(run)
    job = dict(schema='f1-harness-export-job-v2', state='queued', attempts=0,
               processor=processor_identity(),
               connection={k: config[k] for k in ('host', 'port', 'identity', 'known_hosts')},
               receipt=receipt, stop_feedback_confirmed=stop_confirmed,
               experience_root=str(config.get('experience_root',Path(config['root'])/'experience')))
    path = run / JOB_NAME
    if path.exists() or (run/'export-job.json').exists():
        raise FileExistsError('export_job_already_exists')
    save(path, job)
    launch(run.parent)
    return job


def launch(runs_root):
    """Drain each queued release with its own code, then exit; never reuse a daemon.

    V1 workers cannot understand a retirement message or per-job lock. A new
    filename keeps them on their existing exports until deployment retires
    them. They cannot claim jobs intended for this release.
    """
    root = Path(runs_root)
    processors = {}
    for path in root.glob('web-*/' + JOB_NAME):
        try:
            job = json.loads(path.read_text())
            if pending_job(job):
                processor = job['processor']
                processors[processor['id']] = processor
        except (OSError, ValueError, KeyError, TypeError):
            continue
    with (root / 'export-worker.log').open('ab') as log:
        for identity, processor in processors.items():
            env = dict(os.environ, PYTHONPATH=processor['source_root'])
            subprocess.Popen([sys.executable, '-m', 'f1_harness.export_worker', str(root),
                              '--processor', identity], env=env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)


def process_job(path, *, processor=None):
    path = Path(path)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _process_job(path, processor=processor or processor_identity())


def review_collected_run(run, experience_root, *, processor=None):
    """Review already collected evidence without restarting transfer or editing old reviews."""
    from .run_review import summarize_run
    processor = processor or processor_identity()
    run = Path(run)
    review = summarize_run(run, experience_root)
    if review is None:
        raise ValueError('collected_owner_trace_required_for_review')
    value = dict(schema='f1-harness-export-review-v1', processor=processor, review=review)
    identity = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    name = 'experience-review-' + identity + '.json'
    save(run/name, value)
    return dict(status='completed', path=name, processor=processor['id'])


def _process_job(path, *, processor):
    run = path.parent
    job = json.loads(path.read_text())
    if job.get('processor') is not None and job['processor'] != processor:
        return
    # Explicit process_job calls may also retry a failed review after its
    # automatic allowance was used. Workers only select pending_job(job), so a
    # permanently unavailable corpus cannot create an infinite drain loop.
    review=(job.get('experience_review') or {})
    review_only=(job.get('state')=='completed' and job.get('experience_root')
                 and review.get('status')!='completed')
    if job['state'] not in ('queued', 'running') and not review_only:
        return
    output = run / 'collected'
    if (output / 'result.json').is_file():
        result = json.loads((output / 'result.json').read_text())
    else:
        partial = output.with_name(output.name + '.partial')
        if partial.exists():
            partial.rename(run / ('collected.failed-' + str(time.time_ns())))
        job.update(state='running', attempts=job['attempts'] + 1)
        save(path, job)
        try:
            result = collect_rollout(run / 'planner', output, **job['connection'],
                                     metadata_only=True, sealed_receipt=job['receipt'], background=True)
        except Exception as exc:
            job.update(state='queued' if job['attempts'] < 2 else 'failed',
                       error=type(exc).__name__, reason=str(exc)[:500])
            save(path, job)
            EvidenceStore(run).event('workspace_collection_incomplete', title='后台导出待重试' if job['state']=='queued' else '后台导出失败',
                                     level='warn', reason=job['reason'])
            return
    result['stop_feedback_confirmed'] = job['stop_feedback_confirmed']
    save(run/'collection-result.json', result)
    if job['state']!='completed':
        job.update(state='completed', completed_at=time.time(), result=result,
                   completed_by=processor)
        if job.get('experience_root'):
            job['experience_review']=dict(status='queued',attempts=0,processor=processor['id'])
        save(path,job)
        EvidenceStore(run).event('workspace_evidence_collected', title='本次日志已收集，原件保存在F1',
                                 stop_feedback_confirmed=job['stop_feedback_confirmed'])
    if job.get('experience_root') and (job.get('experience_review') or {}).get('status')!='completed':
        attempts=(job.get('experience_review') or {}).get('attempts',0)+1
        job['experience_review']=dict(status='running',attempts=attempts,processor=processor['id'])
        save(path,job)
        try:
            # A supplemental receipt does not overwrite a review made by an
            # older release. It also exposes review failures separately from
            # successful data transfer.
            job['experience_review'] = dict(review_collected_run(run, job['experience_root'], processor=processor),
                                            attempts=attempts)
        except Exception as exc:
            job['experience_review'] = dict(status='queued' if attempts<2 else 'failed',
                                            attempts=attempts,error=type(exc).__name__,processor=processor['id'])
            EvidenceStore(run).event('experience_review_incomplete',error=type(exc).__name__)
        save(path, job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs_root', type=Path)
    parser.add_argument('--processor')
    args = parser.parse_args()
    processor = processor_identity()
    if args.processor is not None and args.processor != processor['id']:
        raise RuntimeError('export_processor_source_changed')
    with (args.runs_root / ('.export-worker-' + processor['id'] + '.lock')).open('a') as lock:
        # Waiting launchers make enqueue-during-exit lossless. The preceding
        # worker finishes its current transfer; the next drains any new jobs.
        fcntl.flock(lock, fcntl.LOCK_EX)
        while True:
            pending = []
            for path in sorted(args.runs_root.glob('web-*/' + JOB_NAME)):
                try:
                    job = json.loads(path.read_text())
                    if job.get('processor') == processor and pending_job(job):
                        pending.append(path)
                except Exception as exc:
                    print(json.dumps(dict(job=str(path), error=type(exc).__name__)), flush=True)
            if not pending:
                return
            for path in pending:
                try:
                    process_job(path, processor=processor)
                except Exception as exc:
                    print(json.dumps(dict(job=str(path), error=type(exc).__name__)), flush=True)
                    # A malformed job must not starve other sealed recordings
                    # or keep a worker alive forever. Retain its original
                    # fields and expose the processing failure for review.
                    try:
                        failed = json.loads(path.read_text())
                        failed.update(state='failed', error=type(exc).__name__,
                                      reason='export_job_processing_failed')
                        save(path, failed)
                    except (OSError, ValueError, TypeError):
                        pass


if __name__ == '__main__':
    main()
