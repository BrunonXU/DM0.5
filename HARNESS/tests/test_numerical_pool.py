"""Real local IPC and service lifecycle, without ROS, model API or robot output."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from f1_harness.contracts import Task, digest
from f1_harness.ipc import UnixClient, UnixServer
from f1_harness.numerical_pool import FixedFiles, NumericalPool, NumericalLease, acquire


def task():
    return Task.model_validate_json((Path(__file__).parents[1] / 'configs/task.example.json').read_text())


class Numeric:
    def __init__(self, spec):
        self.calls = []; self.clears = 0
        self.entered = threading.Event(); self.resume = threading.Event()
    def dispatch(self, method, kwargs):
        self.calls.append((method, deepcopy(kwargs)))
        if kwargs.get('wait'):
            self.entered.set()
            assert self.resume.wait(2)
        return kwargs
    def clear_run(self):
        self.calls.clear(); self.clears += 1


@pytest.fixture
def pool(tmp_path):
    path = tmp_path / 'source'; path.write_text('fixed')
    binding = FixedFiles({path: hashlib.sha256(path.read_bytes()).hexdigest()})
    return NumericalPool({'model': 1}, binding, factory=Numeric)


def begin(pool, value=None):
    result = pool.dispatch('acquire', dict(spec_id=pool.spec_id, binding_id=pool.binding.identity,
                                         task=(value or task()).model_dump(mode='json')))
    return result, {k: result[k] for k in ('token', 'serial', 'task_hash', 'task_id')} if result['acquired'] else None


def test_two_tasks_share_models_but_not_scope_or_results(pool):
    first, lease = begin(pool)
    assert pool.dispatch('call', dict(lease=lease, operation='forward_pose', kwargs={'full22': [1]})) == {'full22': [1]}
    assert not begin(pool)[0]['acquired']
    assert pool.dispatch('release', dict(lease=lease))['reusable']
    second, other = begin(pool, task().model_copy(update={'task_id': 'next-task', 'local_corrections': 0}))
    assert first['token'] != second['token'] and second['serial'] == first['serial']+1
    assert pool.numeric.calls == [] and pool.numeric.clears == 1
    with pytest.raises(ValueError, match='lease_expired'):
        pool.dispatch('call', dict(lease=lease, operation='solve_path', kwargs={}))
    assert pool.dispatch('call', dict(lease=other, operation='solve_path', kwargs={'held16': [2]})) == {'held16': [2]}


def test_release_during_calculation_discards_late_result_and_disables_reuse(pool):
    _, lease = begin(pool)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(pool.dispatch, 'call', dict(lease=lease, operation='solve_path', kwargs={'wait': True}))
        assert pool.numeric.entered.wait(1)
        try:
            assert pool.dispatch('release', dict(lease=lease)) == dict(released=True, reusable=False)
            assert not begin(pool)[0]['acquired']
        finally:
            pool.numeric.resume.set()
        with pytest.raises(ValueError, match='result_revoked'): future.result(timeout=1)
    assert pool.inflight == 0 and not begin(pool)[0]['acquired']


def test_reset_failure_cannot_be_reused(pool):
    _, lease = begin(pool)
    def broken(): raise RuntimeError('reset_failed')
    pool.numeric.clear_run = broken
    with pytest.raises(RuntimeError, match='reset_failed'): pool.dispatch('release', dict(lease=lease))
    assert not begin(pool)[0]['acquired']


def test_changed_source_and_spec_do_not_use_old_models(pool):
    value = dict(spec_id='changed', binding_id=pool.binding.identity, task=task().model_dump(mode='json'))
    assert not pool.dispatch('acquire', value)['acquired']
    next(iter(pool.binding.files)).write_text('edit!')
    assert begin(pool)[0]['reason'] == 'warm_numerical_source_changed'


def test_unregistered_operation_has_no_numeric_side_effect(pool):
    _, lease = begin(pool)
    with pytest.raises(ValueError, match='method_forbidden'):
        pool.dispatch('call', dict(lease=lease, operation='web_start', kwargs={}))
    assert pool.numeric.calls == []


def test_real_private_rpc_keeps_ik_and_fk_independent(tmp_path, pool):
    server = UnixServer(tmp_path/'pool.sock', pool.dispatch).start()
    try:
        _, token = begin(pool)
        lease = NumericalLease(UnixClient(server.path), token, {})
        with ThreadPoolExecutor(1) as executor:
            solve = executor.submit(lease.dispatch, 'solve_path', {'wait': True})
            assert pool.numeric.entered.wait(1)
            try: assert lease.dispatch('forward_pose', {'new': 3}) == {'new': 3}
            finally: pool.numeric.resume.set()
            assert solve.result(timeout=1) == {'wait': True}
        lease.close(); lease.close()
        with pytest.raises(ValueError, match='lease_closed'): lease.dispatch('solve_path', {})
        assert pool.active is None
    finally: server.close()


def test_acquire_binds_actual_new_task_file(tmp_path, pool, monkeypatch):
    (tmp_path/'numerics.local.json').write_text(json.dumps({'spec': {'model': 1}}))
    (tmp_path/'task.local.json').write_text(task().model_dump_json())
    monkeypatch.setattr(FixedFiles, 'release', lambda *a: pool.binding)
    server = UnixServer(tmp_path/'pool.sock', pool.dispatch).start()
    try:
        lease, receipt = acquire(server.path, tmp_path/'numerics.local.json', bundle=tmp_path)
        assert receipt['reused'] and receipt['task_hash'] == digest(task())
        # Warm transport must outlive the existing 15s native search, with an
        # outer envelope larger than its inner RPC; no wall-clock sleep needed.
        from f1_harness.numerical_pool import NUMERICAL_RPC_TIMEOUT_S
        assert 15 < lease.client.timeout < NUMERICAL_RPC_TIMEOUT_S
        assert 'token' not in receipt
        lease.close()
        new = task().model_copy(update={'task_id': 'new-run', 'local_corrections': 0})
        (tmp_path/'task.local.json').write_text(new.model_dump_json())
        lease, receipt = acquire(server.path, tmp_path/'numerics.local.json', bundle=tmp_path)
        assert receipt['task_hash'] == digest(new)
        lease.close()
    finally: server.close()


def test_real_service_routes_warm_and_releases_on_stop(tmp_path, pool, monkeypatch):
    from f1_harness import services
    from f1_harness.evidence import EvidenceStore
    (tmp_path/'numerics.local.json').write_text(json.dumps({'spec': {'model': 1}, 'socket': str(tmp_path/'run.sock')}))
    (tmp_path/'task.local.json').write_text(task().model_dump_json())
    monkeypatch.setattr(FixedFiles, 'release', lambda *a: pool.binding)
    server = UnixServer(tmp_path/'pool.sock', pool.dispatch).start()
    def stop(_):
        assert UnixClient(tmp_path/'run.sock').call('forward_pose', held16=[9]) == {'held16': [9]}
        raise KeyboardInterrupt()
    monkeypatch.setattr(services.time, 'sleep', stop)
    try:
        assert services.serve('numerics', tmp_path/'numerics.local.json', EvidenceStore(tmp_path/'run'), numerical_pool=server.path)['stopped']
        assert pool.active is None and pool.numeric.clears == 1
        assert not (tmp_path/'run.sock').exists()
    finally: server.close()


def test_service_pool_unavailable_uses_original_dispatcher_once(tmp_path, monkeypatch):
    from f1_harness import services
    from f1_harness.evidence import EvidenceStore
    config = tmp_path/'numerics.local.json'; config.write_text(json.dumps({'spec': {'model': 1}, 'socket': str(tmp_path/'run.sock')}))
    monkeypatch.setattr('f1_harness.numerical_pool.acquire', lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    constructed = []
    def factory(spec):
        constructed.append(spec); return Numeric(spec)
    monkeypatch.setattr('f1_harness.numerical_service.NumericalDispatcher', factory)
    monkeypatch.setattr('f1_harness.mesh_cache.register_mesh_cache', lambda *a: None)
    def stop(_):
        assert UnixClient(tmp_path/'run.sock').call('solve_path', held16=[4]) == {'held16': [4]}
        raise KeyboardInterrupt()
    monkeypatch.setattr(services.time, 'sleep', stop)
    assert services.serve('numerics', config, EvidenceStore(tmp_path/'run'), numerical_pool=tmp_path/'absent')['stopped']
    assert constructed == [{'model': 1}]


def test_pool_flag_only_reaches_numerics_service(tmp_path, monkeypatch):
    import sys
    from f1_harness.site import service_commands
    monkeypatch.setattr('f1_harness.site.admission', lambda *a, **k: {})
    (tmp_path/'native').mkdir(); (tmp_path/'native/entry.py').touch()
    rows = service_commands(tmp_path, bundle=tmp_path, python=sys.executable,
                            run=tmp_path/'run', numerical_pool=tmp_path/'pool.sock')
    assert [r['name'] for r in rows if '--numerical-pool' in r['argv']] == ['numerics']
    assert all('owner' not in r['argv'] for r in rows)


def test_prewarm_launch_is_numerical_only_and_filters_credentials(tmp_path, monkeypatch):
    import shlex
    from types import SimpleNamespace
    from f1_harness.numerical_pool_client import WarmNumericalClient
    monkeypatch.setenv('OPENAI_API_KEY', 'never-forward')
    seen = {}
    class Process:
        stdin = SimpleNamespace(close=lambda: seen.update(stdin_closed=True))
        def wait(self, timeout): seen['waited'] = timeout; return 0
    def popen(argv, **kwargs): seen.update(argv=argv, **kwargs); return Process()
    monkeypatch.setattr('f1_harness.numerical_pool_client.subprocess.Popen', popen)
    client = WarmNumericalClient(dict(root=str(tmp_path),host='user@127.0.0.1',port=22009,
        identity='/tmp/key', known_hosts='/tmp/known', local_site='/tmp/template',
        remote_bundle='/home/user/Bruno/HARNESS/development/frozen'),tmp_path)
    endpoint = client.start()
    remote = shlex.split(seen['argv'][-1])
    assert 'numerical-pool' in remote and endpoint in remote
    assert not {'owner','web_start','run-task'} & set(remote)
    assert 'OPENAI_API_KEY' not in seen['env']
    client.close()
    assert seen['stdin_closed'] and seen['waited'] == 15


def test_generated_model_files_are_private_without_changing_requested_spec(tmp_path, pool):
    seen = []
    spec = {'gate': {'runtime': '/original/cold-runtime'}, 'model': 1}
    def factory(value): seen.append(value); return Numeric(value)
    worker = NumericalPool(spec, pool.binding, factory=factory, runtime=tmp_path/'private')
    assert worker.spec_id == digest(spec)
    assert spec['gate']['runtime'] == '/original/cold-runtime'
    assert seen[0]['gate']['runtime'] == str(tmp_path/'private')
