"""No hardware: actual HTTP, single-run arbitration and cancellation ordering."""
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from f1_harness.web_execution import WorkspaceSupervisor, workspace_server
from f1_harness.web_snapshot import checked_request


def request(corrections=2):
    config = dict(mode='astra', instruction='将方块放入方孔。', corrections=corrections, note='')
    revision = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return dict(task_id='square_block', config_revision=revision, config=config)


class Planner:
    returncode = None
    def poll(self):
        return self.returncode


class Processes:
    instances = []
    def __init__(self, config, run):
        self.run = run
        self.calls = []
        self.results = {}
        self.planner = Planner()
        self.authorized = False
        self.epoch = 0
        self.closed = False
        self.prepared = False
        self.instances.append(self)
    def copy_site(self):
        self.calls.append('copy')
    def prepare_boot_recheck(self, stop):
        self.calls.append('boot_recheck')
        return dict(test_fixture=True, motion_authorized=False)
    def start_services(self, stop):
        self.calls.append('services')
    def start_owner(self):
        self.calls.append('owner')
    def check(self):
        pass
    def start_planner(self, request):
        self.calls.append('planner')
        self.prepared = True
        return self.planner
    def owner_call(self, method, **kw):
        self.calls.append(method)
        status = dict(authorized=self.authorized, hold_requested=False, running=None, pending=None,
            task_hash=self.expected_task_hash if self.prepared else None, owner_instance_id='test-owner', epoch=self.epoch,
            admitted_task_hash=self.expected_task_hash, web_control_enabled=True,
            perception_context=dict(session_id='test-session' if self.prepared else None),
            recording_identity=dict(owner_run_id='test-owner-run'))
        if method == 'status':
            return status
        if method == 'web_start':
            assert self.prepared and not self.authorized
            self.authorized = True
            self.epoch += 1
            self.results[kw['request_id']] = 'owner_authorized'
            return dict(lifecycle='queued')
        if method == 'web_stop':
            assert kw['epoch'] == self.epoch
            self.authorized = False
            self.results[kw['request_id']] = 'measured_stationary'
            return dict(lifecycle='queued')
        if method == 'web_control_status':
            return dict(lifecycle='completed', stage=self.results[kw['request_id']], owner=status)
        raise AssertionError(method)
    def stop_planner(self):
        self.calls.append('stop_planner')
        self.planner.returncode = 130
    def close(self):
        self.calls.append('close')
        self.closed = True


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    def fake_freeze(template, target, **kwargs):
        from f1_harness.contracts import Task
        target.mkdir(parents=True)
        return Task(task_id=kwargs['run_name'], object_id='test-block', hole_id='test-hole', calibration_id='test-cal')
    monkeypatch.setattr('f1_harness.web_execution.freeze_site', fake_freeze)
    Processes.instances.clear()
    supervisor = WorkspaceSupervisor(dict(root=str(Path(__file__).resolve().parents[1]),
        runs_root=str(tmp_path / 'runs'), local_site='test-fixture'), processes=Processes)
    yield supervisor
    if supervisor.thread and supervisor.thread.is_alive():
        supervisor.cancel.set()
        supervisor.thread.join(timeout=5)


def test_reads_have_no_process_or_run_side_effects(workspace):
    assert workspace.state()['capabilities']['can_start']
    assert workspace.runs()['runs'] == []
    assert not Processes.instances and not workspace.runs_root.exists()


def test_wait_reason_surfaces_in_workspace_without_ending_task(workspace):
    workspace.start(request())
    wait_until(lambda: workspace.state().get('current_run',{}).get('status')=='running')
    run=Processes.instances[-1].run
    (run/'planner').mkdir(exist_ok=True)
    (run/'planner/progress.json').write_text(json.dumps(dict(state='waiting',skill='approach_grasp',
        reason='capture_faults:source_time_reversed:head',warning=True)))
    wait_until(lambda:'source_time_reversed' in workspace.state()['current_run']['summary'])
    assert workspace.state()['current_run']['status']=='running'
    assert Processes.instances[-1].authorized


def test_shutdown_prevents_a_new_start(workspace):
    workspace.begin_shutdown()
    assert not workspace.state()['capabilities']['can_start']
    with pytest.raises(ValueError):
        workspace.start(request())
    assert not Processes.instances


def test_preparation_is_an_active_cancellable_infra_state(workspace):
    class Preparing(Processes):
        def start_services(self, stop):
            self.calls.append('services')
            assert stop.wait(5)
            raise InterruptedError('cancelled during preparation')
    workspace.processes = Preparing
    started = workspace.start(request())
    wait_until(lambda: bool(Processes.instances))
    current = workspace.state()
    assert started['status'] == current['current_run']['status'] == 'starting'
    assert current['capabilities']['can_stop']
    workspace.stop(dict(run_id=started['run_id']))
    workspace.thread.join(timeout=5)
    assert workspace.state()['current_run']['status'] == 'cancelled'
    assert not any(c in Processes.instances[0].calls for c in ('owner', 'web_start'))


def test_start_is_once_and_stop_waits_for_owner_before_reaping(workspace):
    started = workspace.start(request())
    wait_until(lambda: workspace._metadata(workspace.current)['status'] == 'running')
    child = Processes.instances[0]
    assert child.calls.index('copy') < child.calls.index('boot_recheck') < child.calls.index('services')
    assert child.calls.index('planner') < child.calls.index('web_start')
    with pytest.raises(ValueError, match='不重复启动'):
        workspace.start(request())
    stopped = workspace.stop(dict(run_id=started['run_id']))
    assert stopped['status'] == 'stopping' and not stopped['measured_stationary']
    workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    assert child.calls.index('web_stop') < child.calls.index('stop_planner') < child.calls.index('close')
    assert child.calls.count('web_start') == 1
    current = workspace.state()['current_run']
    assert current['status'] == 'cancelled' and current['stop_confirmation'] == 'measured_stationary'
    assert current['config'] == request()['config']


def test_failed_boot_recheck_never_starts_services_or_owner(workspace):
    class RejectedBoot(Processes):
        def prepare_boot_recheck(self, stop):
            self.calls.append('boot_recheck')
            raise ValueError('boot_recheck_camera_identity_changed')
    workspace.processes=RejectedBoot
    workspace.start(request())
    workspace.thread.join(timeout=5)
    process=Processes.instances[0]
    assert not any(name in process.calls for name in ('services','owner','planner','web_start'))
    assert workspace.state()['current_run']['status']=='error'
    assert 'camera_identity_changed' in workspace.state()['current_run']['summary']


def test_wrong_run_cannot_stop_current_task(workspace):
    workspace.start(request())
    wait_until(lambda: Processes.instances and Processes.instances[0].authorized)
    with pytest.raises(ValueError, match='身份已改变'):
        workspace.stop(dict(run_id='harness-' + '0' * 20))
    assert not workspace.cancel.is_set()


def test_other_owner_is_neither_authorized_nor_stopped(workspace):
    class OtherOwner(Processes):
        def owner_call(self, method, **kw):
            value=super().owner_call(method, **kw)
            if method=='status':
                value['admitted_task_hash']='another-task'
                value['task_hash']='another-task'
            return value
    workspace.processes=OtherOwner
    workspace.start(request())
    workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    process=Processes.instances[0]
    assert 'web_start' not in process.calls and 'web_stop' not in process.calls
    assert process.closed
    assert workspace.state()['current_run']['status']=='error'


def test_post_response_loss_does_not_replay_start(workspace):
    class LostStart(Processes):
        def owner_call(self, method, **kw):
            value = super().owner_call(method, **kw)
            if method == 'web_start':
                raise TimeoutError('lost acknowledgement after acceptance')
            return value
    workspace.processes = LostStart
    workspace.start(request())
    wait_until(lambda: workspace._metadata(workspace.current)['status'] == 'running')
    assert Processes.instances[0].calls.count('web_start') == 1


@pytest.mark.parametrize('before_ack', [False,True])
def test_control_revocation_does_not_leave_ui_running(workspace,before_ack):
    class FaultedOwner(Processes):
        failed=False
        def owner_call(self,method,**kw):
            value=super().owner_call(method,**kw)
            if (self.authorized and not self.failed and
                    method==('web_control_status' if before_ack else 'status')):
                self.failed=True
                self.authorized=False
                self.epoch+=1
            if self.failed:
                state=value.get('owner') if method=='web_control_status' else value if method=='status' else None
                if state is not None:
                    state.update(authorized=False,epoch=self.epoch,
                        authority_revocation=dict(reason='runtime_latched:publisher identity',epoch=self.epoch))
            return value
    workspace.processes=FaultedOwner
    workspace.start(request())
    workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    current=workspace.state()['current_run']
    assert current['status']=='error'
    assert 'runtime_latched:publisher identity' in current['summary']
    process=Processes.instances[0]
    assert process.calls.count('web_start')==1 and process.closed


def test_normal_finish_revocation_is_not_a_runtime_failure(workspace):
    class Finishing(Processes):
        finished=False
        def owner_call(self,method,**kw):
            value=super().owner_call(method,**kw)
            if method=='status' and self.authorized:
                self.finished=True
                self.authorized=False
                self.epoch+=1
                self.planner.returncode=0
            if method=='status' and self.finished:
                value.update(authorized=False,epoch=self.epoch,
                    authority_revocation=dict(reason='task_finished',epoch=self.epoch))
            return value
    workspace.processes=Finishing
    workspace.start(request())
    workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    assert workspace.state()['current_run']['status']=='review'


@pytest.mark.parametrize('exit_code,failure,expected',[
    (2,None,'review'),(0,None,'review'),(1,None,'error'),
    (2,'primitive','error'),(2,'failure_file','error'),(2,'missing_result','error'),
])
def test_pending_review_exit_preserves_reason_and_stop_receipt(workspace,exit_code,failure,expected):
    workspace.start(request())
    wait_until(lambda:workspace._metadata(workspace.current)['status']=='running')
    process=Processes.instances[0]
    planner_dir=process.run/'planner';planner_dir.mkdir(exist_ok=True)
    result=dict(measured={'outcome':'pending_review'},
        attention={'skill':'approach_grasp','reason':'observation_readiness_timeout:scene_expired','submitted':False},
        planner_declaration={'primitive_calls':[{'ok':failure!='primitive','error':'program_failed'}]})
    if failure!='missing_result':(planner_dir/'result.json').write_text(json.dumps(result))
    if failure=='failure_file':(planner_dir/'failure.json').write_text('{"code":"runtime_failure"}')
    process.planner.returncode=exit_code
    workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    current=workspace.state()['current_run']
    assert current['status']==expected
    assert current['stop_confirmation']=='measured_stationary'
    if expected=='review':
        assert '任务待处理' in current['summary'] and 'scene_expired' in current['summary']
        assert 'workspace_task_needs_attention' in (process.run/'trace.jsonl').read_text()
    assert 'web_stop' in process.calls and process.closed


def test_failed_stop_keeps_fault_and_collects_quiescent_failure_evidence(workspace,monkeypatch):
    class Faulted(Processes):
        def start_planner(self,request):
            result=super().start_planner(request)
            (self.run/'planner').mkdir()
            (self.run/'planner/trace.jsonl').write_text('')
            return result
        def owner_call(self,method,**kwargs):
            value=super().owner_call(method,**kwargs)
            if method=='status' and self.authorized:
                self.authorized=False;self.epoch+=1
                value.update(authorized=False,epoch=self.epoch,
                    authority_revocation=dict(reason='runtime_latched:test',epoch=self.epoch))
            return value
        def collect(self):
            assert self.closed
            self.calls.append('collect_quiescent_source')
            return dict(collected=True,training_ready=False)
    def no_stop_feedback(*args,**kwargs):raise TimeoutError('no measured stop')
    workspace.processes=Faulted
    monkeypatch.setattr(workspace,'_stop_owner',no_stop_feedback)
    workspace.start(request());workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    process=Processes.instances[0]
    assert process.calls.index('close')<process.calls.index('collect_quiescent_source')
    state=workspace.state()
    # A new click can prepare a fresh owner, but must not authorize until that
    # owner provides current stationary feedback. The old stop stays unknown.
    assert state['capabilities']['can_start']
    assert workspace.faulted and '重新准备' in state['capabilities']['reason']
    assert state['current_run']['status']=='error'
    assert state['current_run']['stop_confirmation'] is None
    result=json.loads((workspace.current/'collection-result.json').read_text())
    assert result['collected'] and not result['stop_feedback_confirmed']


def test_restart_after_unknown_stop_waits_for_new_owner_measurements(workspace):
    class Recovery(Processes):
        measured=False
        def owner_call(self,method,**kwargs):
            value=super().owner_call(method,**kwargs)
            if method=='status':
                value['restart_readiness']=dict(state='ready' if self.measured else 'waiting',
                    owner_instance_id='test-owner',measured_at_ns=123)
            return value
    workspace.processes=Recovery;workspace.faulted=True;workspace.recovery_of='previous-run'
    started=workspace.start(request())
    wait_until(lambda:Processes.instances and Processes.instances[0].prepared)
    process=Processes.instances[0]
    wait_until(lambda:'实测停稳' in workspace._metadata(workspace.current)['summary'])
    assert 'web_start' not in process.calls and workspace.faulted
    process.measured=True
    wait_until(lambda:workspace._metadata(workspace.current)['status']=='running')
    assert not workspace.faulted and process.calls.count('web_start')==1
    evidence=workspace._metadata(workspace.current)
    assert evidence['recovery_of']=='previous-run' and evidence['restart_evidence']['measured_at_ns']==123
    workspace.stop({'run_id':started['run_id']});workspace.thread.join(timeout=5)
    assert workspace.state()['current_run']['status']=='cancelled'


def test_service_restart_preserves_unconfirmed_stop_and_cancel_cannot_authorize(workspace):
    run=workspace.runs_root/('web-'+'f'*32);run.mkdir(parents=True)
    (run/'workspace-run.json').write_text(json.dumps(dict(id='previous-run',started_at=1,
        status='error',restart_requires_stationary=True)))
    restored=WorkspaceSupervisor(workspace.config,processes=Processes)
    assert restored.faulted and restored.recovery_of=='previous-run'
    started=restored.start(request())
    try:
        wait_until(lambda:Processes.instances and Processes.instances[0].prepared)
        process=Processes.instances[0]
        assert 'web_start' not in process.calls
        restored.stop({'run_id':started['run_id']});restored.thread.join(timeout=5)
        assert not restored.thread.is_alive() and 'web_start' not in process.calls
        assert restored.faulted
    finally:
        restored.cancel.set();restored.thread.join(timeout=5)


@pytest.mark.parametrize('phase',['starting','running','stopping'])
def test_supervisor_crash_does_not_erase_restart_measurement_requirement(workspace,phase):
    run=workspace.runs_root/('web-'+'e'*32);run.mkdir(parents=True)
    (run/'workspace-run.json').write_text(json.dumps(dict(id='interrupted-run',started_at=1,status=phase)))
    restored=WorkspaceSupervisor(workspace.config,processes=Processes)
    assert restored.faulted and restored.recovery_of=='interrupted-run'
    assert restored.state()['capabilities']['can_start']


def test_cancel_queued_start_is_sent_before_authorization(workspace):
    class QueuedStart(Processes):
        def owner_call(self, method, **kw):
            if method == 'web_start':
                self.calls.append(method)
                self.results[kw['request_id']] = 'authorization_requested'
                workspace.cancel.set()
                return dict(lifecycle='queued', stage='authorization_requested')
            return super().owner_call(method, **kw)
    workspace.processes = QueuedStart
    workspace.start(request())
    workspace.thread.join(timeout=5)
    assert not workspace.thread.is_alive()
    process = Processes.instances[0]
    assert not process.authorized
    assert 'web_stop' in process.calls
    assert process.calls.index('web_stop') < process.calls.index('close')
    assert workspace.state()['current_run']['status'] == 'cancelled'


def test_each_run_has_distinct_remote_process_evidence(tmp_path):
    from f1_harness.web_process import WorkspaceProcesses
    # The installed coordinator builds commands without a development CLI in its run root.
    root = tmp_path / 'coordinator'
    root.mkdir()
    config = dict(root=str(root), remote_bundle='/home/user/Bruno/HARNESS/development/test',
        host='user@127.0.0.1', port=22009, identity='/tmp/unused-key', known_hosts='/tmp/unused-hosts')
    groups=[]
    for letter in ('a','b'):
        run=tmp_path/('web-'+letter*32)
        (run/'site').mkdir(parents=True)
        (run/'site/gateway.local.json').write_text(json.dumps(dict(control_port=19840,image_port=19841)))
        (run/'site/masks.local.json').write_text(json.dumps(dict(port=19842)))
        groups.append(WorkspaceProcesses(config,run))
    assert groups[0].instructions['remote_services_run'] != groups[1].instructions['remote_services_run']
    assert groups[0].run.name in groups[0].instructions['remote_services_run']


def test_http_requires_proxy_and_get_cannot_authorize(workspace):
    server = workspace_server(workspace, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    def call(method, path, body=None, **headers):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()
    try:
        assert call('GET', '/state')[0] == 200
        assert not Processes.instances
        assert call('POST', '/start', request(), **{'Content-Type': 'application/json'})[0] == 403
        assert not Processes.instances
        assert call('POST', '/start', request(), **{'Content-Type': 'application/json',
            'X-F1-Harness': 'workspace', 'Origin': 'https://outside.example'})[0] == 403
        status, result = call('POST', '/start', request(), **{'Content-Type': 'application/json',
            'X-F1-Harness': 'workspace'})
        assert status == 200 and result['status'] == 'starting'
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('corrections', [0, 1, 2])
def test_reviewed_budget_can_be_reduced(corrections):
    assert checked_request(request(corrections))['config']['corrections'] == corrections


def test_config_revision_prevents_unreviewed_request_change():
    body = request()
    body['config']['instruction'] = 'changed task'
    with pytest.raises(ValueError, match='身份不匹配'):
        checked_request(body)


def test_llm_read_scopes_run_and_uses_web_start_clock(workspace):
    from f1_harness.evidence import EvidenceStore
    from f1_harness.llm_trace import capture
    root = workspace.runs_root / ('web-' + 'a'*32)
    store = EvidenceStore(root/'planner')
    started = time.monotonic_ns() - 10_000_000_000
    (root/'workspace-run.json').write_text(json.dumps(dict(started_ns=started)))
    rid = workspace.run_id(root.name)
    capture(store, 'b'*32, 'request', dict(model='fixture', input='full input'))
    rows = workspace.llm(rid)['exchanges']
    assert 10 <= rows[0]['time_s'] < 12
    assert workspace.llm(rid, 'b'*32)['request']['content']['input'] == 'full input'
    with pytest.raises(ValueError): workspace.llm('harness-'+'0'*20)
    assert not Processes.instances


def test_explicit_stuck_preserves_summary_without_attention(tmp_path):
    from f1_harness.web_execution import WorkspaceSupervisor
    run=tmp_path; (run/'planner').mkdir()
    (run/'planner/result.json').write_text(json.dumps(dict(measured={'outcome':'pending_review'},
        attention=None,planner_declaration={'status':'stuck','summary':'table collision; no grasp executed'})))
    status,summary,attention=WorkspaceSupervisor._planner_outcome(run,2)
    assert status=='review' and 'table collision' in summary
    assert attention['motion_verified'] is False
