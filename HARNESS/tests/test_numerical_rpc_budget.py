"""Actual nested Unix RPC; arithmetic is controlled, with no robot or model."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading

import pytest

from f1_harness.contracts import Task
from f1_harness.ipc import UnixClient, UnixServer
from f1_harness.numerical_pool import FixedFiles, NumericalLease, NumericalPool
from f1_harness.owner_bridge import numerical_failure_detail
from test_numerical_service import dispatcher


@contextmanager
def chain(tmp_path, dispatcher, *, inner_timeout, pool_clients=6):
    source = tmp_path/'bound-source'; source.write_text('fixed test source')
    pool = NumericalPool({}, FixedFiles({source: hashlib.sha256(source.read_bytes()).hexdigest()}),
                         factory=lambda spec: dispatcher)
    dispatcher.clear_run = lambda: None
    task = Task.model_validate_json((Path(__file__).parents[1]/'configs/task.example.json').read_text())
    acquired = pool.dispatch('acquire', dict(spec_id=pool.spec_id, binding_id=pool.binding.identity,
                                           task=task.model_dump()))
    token = {key: acquired[key] for key in ('token', 'serial', 'task_hash', 'task_id')}
    finished = threading.Event(); late = []
    def worker(method, kwargs):
        try:return pool.dispatch(method, kwargs)
        except Exception as exc:
            if method == 'call':late.append(str(exc))
            raise
        finally:
            if method == 'call':finished.set()
    # Server socket timeouts limit reads/writes, never synchronous dispatch.
    inner = UnixServer(tmp_path/'pool.sock', worker, timeout=.02, max_clients=pool_clients).start()
    lease = NumericalLease(UnixClient(inner.path, timeout=inner_timeout), token, {})
    outer = UnixServer(tmp_path/'run.sock', lease.dispatch, timeout=.02).start()
    try:
        yield pool, lease, UnixClient(outer.path, timeout=1.), finished, late
    finally:
        dispatcher.numerics.resume.set()
        finished.wait(1)
        lease.close()
        outer.close(); inner.close()


@pytest.mark.parametrize('native_failure', [False, True])
def test_nested_rpc_waits_for_computation_and_keeps_native_diagnostic(tmp_path, dispatcher, native_failure):
    native = dispatcher.numerics
    def solve(**kwargs):
        native.entered.set()
        assert native.resume.wait(1)
        if native_failure:
            raise ValueError('numerical_joint_search_no_valid_goal:'+json.dumps(dict(
                search_phase='endpoint_native', position_error_m=.003, native_valid_goal_count=0)))
        return [[42.]*16]
    native.solve_path = solve
    with chain(tmp_path, dispatcher, inner_timeout=.6) as (pool, lease, client, finished, late):
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(client.call, 'solve_path')
            assert native.entered.wait(1)
            # A reduced clock fixture crosses the server I/O timeout while
            # staying inside both client envelopes, without a 15-second sleep.
            timer = threading.Timer(.12, native.resume.set); timer.start()
            try:
                if native_failure:
                    with pytest.raises(RuntimeError) as caught:future.result(timeout=1)
                    detail = numerical_failure_detail(caught.value)
                    assert detail['code'] == 'numerical_joint_search_no_valid_goal'
                    assert detail['metrics']['search_phase'] == 'endpoint_native'
                    assert detail['metrics']['position_error_m'] == .003
                else:assert future.result(timeout=1) == [[42.]*16]
            finally:timer.cancel(); native.resume.set(); timer.join()
        assert finished.wait(1) and not lease.closed and not pool.poisoned
        assert not dispatcher.solve_lock.locked()
        assert client.call('forward_pose', full22=[0.]*22, held16=[0.]*16)


@pytest.mark.parametrize('release_reaches_worker', [False, True])
def test_timeout_closes_lease_without_replay_or_hiding_original_error(tmp_path, dispatcher,
                                                                   release_reaches_worker):
    native = dispatcher.numerics; calls = []
    def solve(**kwargs):
        calls.append(kwargs); native.entered.set()
        assert native.resume.wait(2)
        return [[42.]*16]
    native.solve_path = solve
    with chain(tmp_path, dispatcher, inner_timeout=.1,
               pool_clients=6 if release_reaches_worker else 1) as (pool, lease, client, finished, late):
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(client.call, 'solve_path')
            assert native.entered.wait(1)
            with pytest.raises(RuntimeError, match='TimeoutError: timed out'):
                future.result(timeout=1)
        assert lease.closed and dispatcher.solve_lock.locked() and len(calls) == 1
        with pytest.raises(RuntimeError, match='warm_numerical_lease_closed'):
            client.call('solve_path')
        assert len(calls) == 1  # No second native invocation, including cold fallback.
        if release_reaches_worker:
            assert pool.poisoned and pool.active is None
        else:
            # Occupied server slots make revocation fail. The original timeout
            # survives; the closed local lease and occupied pool prevent reuse.
            assert pool.active is not None
        assert pool.dispatch('acquire', {}) == dict(acquired=False, reason='warm_numerical_unavailable')
        native.resume.set()
        assert finished.wait(1) and not dispatcher.solve_lock.locked()
        assert pool.inflight == 0 and len(calls) == 1
        if release_reaches_worker:assert late == ['warm_numerical_result_revoked']

