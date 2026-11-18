import json
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
from f1_harness import export_worker


def test_interrupted_copy_retries_without_overwriting_original_partial(tmp_path,monkeypatch):
    run=tmp_path/'web-run';run.mkdir();(run/'collected.partial').mkdir()
    (run/'collected.partial/diagnostic').write_text('preserve')
    job=run/'export-job.json';export_worker.save(job,dict(state='running',attempts=1,
        connection={},receipt={'seal_sha256':'a'*64},stop_feedback_confirmed=False))
    calls=[]
    def copy(planner,output,**kwargs):
        assert kwargs['metadata_only'] and kwargs['background']
        assert not kwargs.get('seal') and not kwargs.get('render_video')
        assert kwargs['sealed_receipt']=={'seal_sha256':'a'*64}
        assert not (run/'collected.partial').exists()
        assert list(run.glob('collected.failed-*/diagnostic'))[0].read_text()=='preserve'
        output.mkdir();result=dict(collected=True,seal=dict(autonomous_success=False))
        (output/'result.json').write_text(json.dumps(result));calls.append(output)
        return result
    monkeypatch.setattr(export_worker,'collect_rollout',copy)
    export_worker.process_job(job)
    assert json.loads(job.read_text())['state']=='completed'
    assert not json.loads((run/'collection-result.json').read_text())['stop_feedback_confirmed']
    # Crash after result write but before marking the durable job completed.
    state=json.loads(job.read_text());state['state']='running';export_worker.save(job,state)
    export_worker.process_job(job)
    assert len(calls)==1 and json.loads(job.read_text())['state']=='completed'


def test_failed_export_is_bounded_and_cannot_change_motion_result(tmp_path,monkeypatch):
    run=tmp_path/'web-run';run.mkdir();job=run/'export-job.json'
    export_worker.save(job,dict(state='queued',attempts=0,connection={},receipt={},stop_feedback_confirmed=True))
    def fail(*a,**k):raise OSError('copy unavailable')
    monkeypatch.setattr(export_worker,'collect_rollout',fail)
    export_worker.process_job(job);assert json.loads(job.read_text())['state']=='queued'
    export_worker.process_job(job);assert json.loads(job.read_text())['state']=='failed'
    assert not (run/'collection-result.json').exists()


def test_new_jobs_are_invisible_to_legacy_exporter_and_bind_their_processor(tmp_path, monkeypatch):
    run = tmp_path/'web-current'; run.mkdir()
    launched = []
    monkeypatch.setattr(export_worker, 'launch', lambda root: launched.append(root))
    job = export_worker.enqueue(run, dict(root=str(tmp_path), host='unused', port=1,
        identity='unused', known_hosts='unused'), {}, stop_confirmed=True)
    assert job['processor'] == export_worker.processor_identity()
    assert not list(tmp_path.glob('web-*/export-job.json'))
    assert export_worker.job_path(run).name == export_worker.JOB_NAME
    assert launched == [tmp_path]
    other = dict(job['processor'], id='other-release')
    export_worker.process_job(export_worker.job_path(run), processor=other)
    assert json.loads(export_worker.job_path(run).read_text())['state'] == 'queued'


def test_release_workers_finish_inflight_jobs_and_exit_without_using_legacy_lock(tmp_path):
    """Real child processes and locks, with already-collected test data and no network."""
    source = Path(export_worker.__file__).resolve().parent
    release = tmp_path/'other-release'
    shutil.copytree(source, release/'f1_harness', ignore=shutil.ignore_patterns('__pycache__'))
    other_env = dict(os.environ, PYTHONPATH=str(release), OPENBLAS_NUM_THREADS='1')
    current_env = dict(os.environ, PYTHONPATH=str(source.parent), OPENBLAS_NUM_THREADS='1')
    other = json.loads(subprocess.check_output([sys.executable, '-c',
        'import json; from f1_harness.export_worker import processor_identity; print(json.dumps(processor_identity()))'],
        env=other_env, text=True))
    current = export_worker.processor_identity()
    assert other['id'] != current['id']
    runs = tmp_path/'runs'; runs.mkdir()

    def job(name, processor):
        run = runs/name; (run/'collected').mkdir(parents=True)
        (run/'collected/result.json').write_text('{"collected":true}')
        path = run/export_worker.JOB_NAME
        export_worker.save(path, dict(state='queued', attempts=0, processor=processor,
            stop_feedback_confirmed=True))
        return path

    first, other_job = job('web-first', current), job('web-other', other)
    children = []

    def start(env, identity):
        process = subprocess.Popen([sys.executable, '-m', 'f1_harness.export_worker', str(runs),
            '--processor', identity], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(process)
        return process

    # Simulate an older daemon still owning its lock, and one current transfer
    # in flight. Another release must finish its own queue without killing it.
    try:
        with (runs/'.export-worker.lock').open('a') as legacy, first.with_suffix('.lock').open('a') as busy:
            fcntl.flock(legacy, fcntl.LOCK_EX)
            fcntl.flock(busy, fcntl.LOCK_EX)
            active = start(current_env, current['id'])
            independent = start(other_env, other['id'])
            stdout, stderr = independent.communicate(timeout=15)
            assert independent.returncode == 0, (stdout, stderr)
            assert active.poll() is None
            assert json.loads(other_job.read_text())['completed_by'] == other
            later = job('web-later', current)
            waiter = start(current_env, current['id'])
            fcntl.flock(busy, fcntl.LOCK_UN)
            for process in (active, waiter):
                stdout, stderr = process.communicate(timeout=15)
                assert process.returncode == 0, (stdout, stderr)
        for path in (first, later):
            value = json.loads(path.read_text())
            assert value['state'] == 'completed' and value['completed_by'] == current
            events = [json.loads(line) for line in (path.parent/'trace.jsonl').read_text().splitlines()]
            assert sum(row['kind'] == 'workspace_evidence_collected' for row in events) == 1
        assert not list(runs.glob('web-*/export-job.json'))
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate(); child.communicate(timeout=5)


def test_export_path_runs_current_independent_program_verifier(tmp_path):
    from test_program_trials import registered, trial_run
    from f1_harness.memory import ExperienceStore
    from f1_harness.program_experience import pending_agent_programs

    root, key, task = registered(tmp_path)
    candidate = pending_agent_programs(root, task, synthetic=True)[0]
    run = tmp_path/'web-independent'
    trial_run(run, candidate=candidate['trial_candidate'])
    (run/'collected/result.json').write_text('{"collected":true}')
    job = run/export_worker.JOB_NAME
    export_worker.save(job, dict(state='queued', attempts=0, processor=export_worker.processor_identity(),
        experience_root=str(root), stop_feedback_confirmed=True))
    export_worker.process_job(job)
    value = json.loads(job.read_text())
    assert value['state'] == 'completed' and value['experience_review']['status'] == 'completed'
    review = json.loads((run/value['experience_review']['path']).read_text())
    assert review['review']['agent_programs']['independent_validation']['promoted'] == [key]
    assert ExperienceStore(root, synthetic=True).snapshot()['count'] == 1


def test_transient_review_failure_retries_without_recollecting(tmp_path,monkeypatch):
    run=tmp_path/'web-review';run.mkdir();job=run/export_worker.JOB_NAME
    export_worker.save(job,dict(state='queued',attempts=0,connection={},receipt={},
        stop_feedback_confirmed=True,experience_root=str(tmp_path/'memory')))
    transfers=[];reviews=[]
    def collect(planner,output,**kwargs):
        transfers.append(True);output.mkdir()
        result=dict(collected=True)
        (output/'result.json').write_text(json.dumps(result))
        return result
    def review(*args,**kwargs):
        reviews.append(True)
        if len(reviews)==1:raise OSError('temporary review storage failure')
        return dict(status='completed',path='synthetic-review.json',processor=kwargs['processor']['id'])
    monkeypatch.setattr(export_worker,'collect_rollout',collect)
    monkeypatch.setattr(export_worker,'review_collected_run',review)
    export_worker.process_job(job)
    first=json.loads(job.read_text())
    assert first['state']=='completed' and first['experience_review']['status']=='queued'
    assert export_worker.pending_job(first)
    collected=(run/'collection-result.json').read_bytes()
    export_worker.process_job(job)
    final=json.loads(job.read_text())
    assert not export_worker.pending_job(final)
    assert final['experience_review']['status']=='completed' and final['experience_review']['attempts']==2
    assert len(transfers)==1 and len(reviews)==2
    assert (run/'collection-result.json').read_bytes()==collected
    rows=[json.loads(line) for line in (run/'trace.jsonl').read_text().splitlines()]
    assert sum(row['kind']=='workspace_evidence_collected' for row in rows)==1


def test_permanent_review_failure_stops_automatic_loop_but_explicit_retry_remains_possible(tmp_path,monkeypatch):
    run=tmp_path/'web-review';(run/'collected').mkdir(parents=True)
    (run/'collected/result.json').write_text('{"collected":true}')
    job=run/export_worker.JOB_NAME
    export_worker.save(job,dict(state='queued',attempts=0,stop_feedback_confirmed=True,
        experience_root=str(tmp_path/'memory')))
    def unavailable(*args,**kwargs):raise OSError('unavailable')
    monkeypatch.setattr(export_worker,'review_collected_run',unavailable)
    for _ in range(2):export_worker.process_job(job)
    failed=json.loads(job.read_text())
    assert failed['state']=='completed' and failed['experience_review']['status']=='failed'
    assert not export_worker.pending_job(failed)
    monkeypatch.setattr(export_worker,'review_collected_run',lambda *a,**k:dict(status='completed'))
    export_worker.process_job(job)
    assert json.loads(job.read_text())['experience_review']==dict(status='completed',attempts=3)
