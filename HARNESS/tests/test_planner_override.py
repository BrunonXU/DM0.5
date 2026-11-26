"""H-28: workspace planner_override='baseline', planner_effective snapshot, and run_funnel."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from f1_harness.contracts import Task, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.web_execution import WorkspaceSupervisor
from f1_harness.web_process import WorkspaceProcesses


def _request(mode='astra', corrections=2):
    config = dict(mode=mode, instruction='将方块放入方孔。', corrections=corrections, note='')
    revision = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return dict(task_id='square_block', config_revision=revision, config=config)


def _load_run_funnel():
    script = Path(__file__).resolve().parents[1] / 'scripts/run_funnel.py'
    spec = importlib.util.spec_from_file_location('f1_harness_run_funnel_h28', script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harness_root(tmp_path):
    real_root = Path(__file__).resolve().parents[1]
    if (real_root.parent / 'F1-4242/task_service.py').is_file():
        return real_root
    # Support sparse checkouts where sibling F1-4242/task_service.py is absent.
    ws = tmp_path / '_ws'
    harness = ws / 'HARNESS'
    f1_4242 = ws / 'F1-4242'
    if not harness.exists():
        harness.mkdir(parents=True)
        for sub in ('scripts', 'configs', 'src'):
            (harness / sub).symlink_to(real_root / sub)
        f1_4242.mkdir(parents=True)
        (f1_4242 / 'task_service.py').write_text(
            'DELTA_SCHEMA = "delta-v1"\n'
            'class TaskService:\n'
            '    def __init__(self, root, offline=True): self.root = root\n'
            '    def _read_run(self, *a, **kw): return None\n'
            '    def _trace(self, *a, **kw): return [], []\n'
            'def _text(v): return str(v)\n'
            'def _clean(v): return v\n',
            encoding='utf-8',
        )
    return harness


def _base_config(tmp_path, **extra):
    root = _harness_root(tmp_path)
    cfg = dict(
        root=str(root),
        runs_root=str(tmp_path / 'runs'),
        local_site=str(tmp_path / 'site-template'),
        remote_bundle='/home/user/Bruno/HARNESS/development/test',
        host='user@127.0.0.1',
        port=22009,
        identity=str(tmp_path / 'id_ed25519'),
        known_hosts=str(tmp_path / 'known_hosts'),
        experience_root=str(tmp_path / 'experience'),
    )
    cfg.update(extra)
    return cfg


def _write_minimal_site(run_dir: Path):
    site = run_dir / 'site'
    site.mkdir(parents=True, exist_ok=True)
    (site / 'gateway.local.json').write_text(
        json.dumps({'control_port': 19840, 'image_port': 19841, 'scene': {'mask_execution': 'remote'}}),
        encoding='utf-8',
    )
    (site / 'masks.local.json').write_text(json.dumps({'port': 19842}), encoding='utf-8')


def test_invalid_planner_override_rejected_before_service_start(tmp_path):
    for bad in ('gemini', 'astra', '', 123, True):
        with pytest.raises(ValueError, match='invalid_planner_override'):
            WorkspaceSupervisor(_base_config(tmp_path, planner_override=bad))
        with pytest.raises(ValueError, match='invalid_planner_override'):
            WorkspaceProcesses(_base_config(tmp_path, planner_override=bad), tmp_path / 'run-x')


def test_start_planner_override_baseline_vs_default_exact_argv(tmp_path):
    run_default = tmp_path / 'run-default'
    run_override = tmp_path / 'run-override'
    _write_minimal_site(run_default)
    _write_minimal_site(run_override)

    spawned = []

    class CapturingProcesses(WorkspaceProcesses):
        def spawn(self, row):
            spawned.append(row)
            return row

    p_default = CapturingProcesses(_base_config(tmp_path), run_default)
    p_default.start_planner(_request('astra'))
    argv_default = spawned[-1]['argv']
    idx = argv_default.index('--planner')
    assert argv_default[idx + 1] == 'astra'

    p_override = CapturingProcesses(_base_config(tmp_path, planner_override='baseline'), run_override)
    p_override.start_planner(_request('astra'))
    argv_override = spawned[-1]['argv']
    idx_o = argv_override.index('--planner')
    assert argv_override[idx_o + 1] == 'baseline'

    # Unrelated arguments match word-for-word (modulo run directory)
    norm_default = [s.replace(str(run_default), '<RUN>') for s in argv_default]
    norm_override = [s.replace(str(run_override), '<RUN>') for s in argv_override]
    norm_override[idx_o + 1] = 'astra'
    assert norm_default == norm_override


def test_vla_astra_with_planner_override_rejected_before_freeze_or_owner(tmp_path, monkeypatch):
    freeze_called = []
    monkeypatch.setattr('f1_harness.web_execution.freeze_site', lambda *a, **kw: freeze_called.append(True))
    supervisor = WorkspaceSupervisor(_base_config(tmp_path, planner_override='baseline'))
    with pytest.raises(ValueError, match='planner_override_incompatible_with_vla_astra'):
        supervisor.start(_request('vla_astra'))
    assert not freeze_called
    assert supervisor.current is None

    run_vla = tmp_path / 'run-vla'
    _write_minimal_site(run_vla)
    proc = WorkspaceProcesses(_base_config(tmp_path, planner_override='baseline'), run_vla)
    with pytest.raises(ValueError, match='planner_override_incompatible_with_vla_astra'):
        proc.start_planner(_request('vla_astra'))


def test_workspace_run_snapshot_records_planner_effective_and_run_funnel_reads_it(tmp_path, monkeypatch):
    class DummyProcesses:
        def __init__(self, config, run):
            self.run = run
        def copy_site(self):
            raise InterruptedError('stop_early_for_snapshot_check')
        def close(self):
            pass

    def fake_freeze(template, target, **kwargs):
        target.mkdir(parents=True)
        return Task(task_id=kwargs['run_name'], object_id='test-block', hole_id='test-hole', calibration_id='test-cal')

    monkeypatch.setattr('f1_harness.web_execution.freeze_site', fake_freeze)

    # 1. With planner_override='baseline'
    sup_base = WorkspaceSupervisor(_base_config(tmp_path / 'b', planner_override='baseline'), processes=DummyProcesses)
    sup_base.start(_request('astra'))
    sup_base.thread.join(timeout=5)
    meta_base = json.loads((sup_base.current / 'workspace-run.json').read_text(encoding='utf-8'))
    assert meta_base['config']['mode'] == 'astra'
    assert meta_base['planner_effective'] == 'baseline'

    # 2. Without planner_override
    sup_def = WorkspaceSupervisor(_base_config(tmp_path / 'd'), processes=DummyProcesses)
    sup_def.start(_request('astra'))
    sup_def.thread.join(timeout=5)
    meta_def = json.loads((sup_def.current / 'workspace-run.json').read_text(encoding='utf-8'))
    assert meta_def['config']['mode'] == 'astra'
    assert meta_def['planner_effective'] == 'astra'

    # 3. run_funnel prefers planner_effective, falls back to config.mode when absent
    funnel = _load_run_funnel()
    task = dict(task_id='trial', goal='placed')
    owner = [
        dict(kind='run_identity', event_id='e1', monotonic_ns=1_000_000_000, synthetic=False),
        dict(kind='task_prepared', event_id='e2', monotonic_ns=2_000_000_000, session_id='A', task_hash='h', task=task),
        dict(kind='web_authorization', event_id='e3', monotonic_ns=3_000_000_000, session_id='A', task_hash='h', task_id='trial', synthetic=False),
        dict(kind='authority_revoked', event_id='e4', monotonic_ns=4_000_000_000, reason='task_finished'),
    ]
    planner = [dict(kind='remote_task_prepared', event_id='p1', monotonic_ns=1_000_000_000, session_id='A', task=task)]
    table = {'physical_exact': [], 'physical_prefix': []}

    for name, snap, expected_mode in (
        ('r-override', {'config': {'mode': 'astra'}, 'planner_effective': 'baseline'}, 'baseline'),
        ('r-legacy', {'config': {'mode': 'astra'}}, 'astra'),
    ):
        d = tmp_path / name
        d.mkdir()
        (d / 'owner-trace.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in owner), encoding='utf-8')
        (d / 'planner-trace.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in planner), encoding='utf-8')
        (d / 'workspace-run.json').write_text(json.dumps(snap), encoding='utf-8')
        res = funnel.analyze_run(d, table, {})
        assert res['mode'] == expected_mode
        assert res['model_calls'] == 0
        summary = funnel.summarize([res])
        assert summary['mode_counts'][expected_mode] == 1


def test_run_task_baseline_hardware_never_creates_model_client_or_reads_credentials(tmp_path, monkeypatch):
    from f1_harness import task_runner

    # Poison ResponsesPlanner so any construction fails loudly
    monkeypatch.setattr(
        'f1_harness.planner.ResponsesPlanner',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('model client must not be created')),
    )

    task_obj = Task(task_id='t1', object_id='b1', hole_id='h1', calibration_id='c1')
    task_hash = digest(task_obj)

    class FakeBackend:
        def __init__(self, gateway, task, store, image_gateway):
            self.task = task
            self.instance = 'owner-1'
            self.session = 'sess-1'
            self.pending = None
            self.defer_session_close = False
            self.observation_snapshot_supported = True
            self.task_deadline = None
            self.attention = None
            self.placement_verified = True
            self.prepared_authority = {
                'task_hash': task_hash,
                'authorized': False,
                'epoch': 0,
                'task_generation': 1,
            }
        def call(self, method, **kwargs):
            if method == 'capabilities':
                return {'mode': 'hardware', 'hardware_ready': True, 'consistent_observation_snapshot': True}
            if method == 'status':
                return {
                    'authorized': True,
                    'hardware_ready': True,
                    'epoch': 1,
                    'owner_instance_id': 'owner-1',
                    'task_hash': task_hash,
                    'recording_identity': {'owner_run_id': 'r1'},
                    'perception_context': {
                        'session_id': 'sess-1',
                        'owner_instance_id': 'owner-1',
                        'task_id': task_obj.task_id,
                        'task_hash': task_hash,
                        'task_generation': 1,
                        'epoch': 1,
                    },
                }
            if method == 'execution_events':
                return {'events': [], 'next': 0, 'complete': True, 'high_watermark': 0}
            if method == 'finish_task':
                return {'outcome': 'success'}
            raise AssertionError(method)
        def heartbeat(self):
            pass
        def _progress(self, *a, **kw):
            pass
        def solved(self):
            return True
        def cancel(self):
            pass
        def close_images(self):
            pass

    class FakeArm:
        turns = 1
        def __init__(self, backend, root, **kw):
            pass
        def start(self):
            pass
        def execute_tool(self, name, payload):
            return argparse.Namespace(result={'outcome': 'success', 'model_requests': 0})
        def close(self):
            pass

    monkeypatch.setattr(task_runner, 'EvidenceRemoteBackend', FakeBackend)
    monkeypatch.setattr(task_runner, 'SandboxedArm', FakeArm)

    task_path = tmp_path / 'task.json'
    task_path.write_text(task_obj.model_dump_json(), encoding='utf-8')
    # Nonexistent/unreadable credential path: must never be opened
    missing_cred = tmp_path / 'MUST_NOT_BE_READ_API.MD'
    store = EvidenceStore(tmp_path / 'run')
    args = argparse.Namespace(
        task=task_path,
        workspace_config=None,
        authorization_source='web',
        gateway='http://127.0.0.1:19840',
        image_gateway='http://127.0.0.1:19841',
        start_timeout=5.0,
        planner='baseline',
        experience_snapshot=None,
    )
    result = task_runner.run_task(args, store, missing_cred)
    assert result['measured']['outcome'] == 'success'
    assert result['planner_declaration']['model_requests'] == 0
