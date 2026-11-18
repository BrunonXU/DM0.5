"""Actual Unix RPC concurrency, using isolated numerical fixtures and no outputs."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.ipc import UnixClient, UnixServer
from f1_harness.numerical_service import NumericalDispatcher, NUMERICAL_CLIENTS, FORWARD_LOCK_TIMEOUT_S


@pytest.fixture
def dispatcher(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'DAgger/F1/src'))
    from f1dagger.contract import MODEL_NAMES, vector

    class Mapping:
        def __init__(self):
            self.signs = np.arange(22) % 2 * 2 - 1
            self.limits = np.full((14, 2), [-90., 90.])

        def check_action(self, value):
            result = vector(value, 16)
            if np.any(np.abs(result[:14]) > 90) or np.any(result[14:] < 0) or np.any(result[14:] > 100):
                raise ValueError('out_of_bounds')
            return result

        def to_model(self, value):
            return np.deg2rad(vector(value, 22) * self.signs)

    class Model:
        nframes = 1

        def __init__(self):
            self.lowerPositionLimit = np.zeros(22)

        def createData(self):
            return SimpleNamespace(oMf=[SimpleNamespace(rotation=np.eye(3), translation=np.zeros(3))],
                                   q=np.zeros(22), calls=0)

        def getFrameId(self, name):
            assert name == 'arm_R_Link7'
            return 0

    class Pin:
        def __init__(self):
            self.entered, self.resume = threading.Event(), threading.Event()
            self.pause = False

        def neutral(self, model):
            return np.zeros(22)

        def forwardKinematics(self, model, data, q):
            self.entered.set()
            if self.pause:
                assert self.resume.wait(3)
            data.q = q.copy()
            data.calls += 1

        def updateFramePlacements(self, model, data):
            data.oMf[0].translation[:] = data.q[[7, 14, 21]]

    class Arm:
        def __init__(self, urdf, mapping, ik):
            self.full_model, self.pin, self.mapping = Model(), Pin(), mapping
            self.virtual_q = np.zeros(7)

        def _set(self, q, name, value):
            q[MODEL_NAMES.index(name)] = value

    class Numerics:
        def __init__(self, spec):
            self.mapping = Mapping()
            self.arm = Arm(spec['urdf'], self.mapping, spec['ik'])
            self.data = self.arm.full_model.createData()
            self.entered, self.resume = threading.Event(), threading.Event()

        def solve_path(self, **kwargs):
            self.arm.virtual_q[:] = 42
            self.data.q[:] = 19
            self.entered.set()
            if not self.resume.wait(3):
                raise TimeoutError('fixture_solve_deadline')
            return [[42] * 16]

    return NumericalDispatcher({'urdf': 'fixture.urdf', 'ik': {}},
                               numerics_factory=Numerics, arm_factory=Arm)


def test_long_unix_solve_does_not_block_fk_or_queue_second_solve(tmp_path, dispatcher):
    server = UnixServer(tmp_path / 'numerics.sock', dispatcher.dispatch,
                        max_clients=NUMERICAL_CLIENTS).start()
    client = UnixClient(server.path, timeout=4)
    with ThreadPoolExecutor(max_workers=1) as executor:
        solve = executor.submit(client.call, 'solve_path', goals=[], gripper=None, full22=[0.] * 22, held16=[0.] * 16)
        try:
            assert dispatcher.numerics.entered.wait(1)
            for _ in range(3):
                started = time.monotonic()
                pose = client.call('forward_pose', full22=[0.] * 22, held16=[0.] * 16)
                assert np.allclose(pose, np.eye(4)) and time.monotonic() - started < .5
                assert not solve.done()
            started = time.monotonic()
            with pytest.raises(RuntimeError, match='numerical_solver_busy'):
                client.call('solve_path', goals=[])
            assert time.monotonic() - started < .5 and not solve.done()
            poses = client.call('forward_path', full22=[0.] * 22, targets=[[0.] * 16] * 100)
            assert len(poses) == 100 and not solve.done()
        finally:
            dispatcher.numerics.resume.set()
            assert solve.result(timeout=2) == [[42] * 16]
            server.close()


def test_forward_lock_overload_is_bounded_and_does_not_block_solver(tmp_path, dispatcher):
    server = UnixServer(tmp_path / 'numerics.sock', dispatcher.dispatch,
                        max_clients=NUMERICAL_CLIENTS).start()
    client = UnixClient(server.path, timeout=4)
    pin = dispatcher.forward.pin
    pin.pause = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.call, 'forward_pose', full22=[0.] * 22, held16=[0.] * 16)
        try:
            assert pin.entered.wait(1)
            started = time.monotonic()
            with pytest.raises(RuntimeError, match='numerical_forward_busy'):
                client.call('forward_path', full22=[0.] * 22, targets=[[0.] * 16])
            elapsed = time.monotonic() - started
            assert FORWARD_LOCK_TIMEOUT_S * .9 <= elapsed < .5
            assert not pending.done()
            dispatcher.numerics.resume.set()
            assert client.call('solve_path') == [[42] * 16]
        finally:
            pin.resume.set()
            assert np.allclose(pending.result(timeout=2), np.eye(4))
            server.close()


def test_frame_fk_waits_for_short_batch_without_losing_observation(tmp_path, dispatcher):
    server = UnixServer(tmp_path / 'numerics.sock', dispatcher.dispatch,
                        max_clients=NUMERICAL_CLIENTS).start()
    client = UnixClient(server.path, timeout=2)
    pin = dispatcher.forward.pin
    pin.pause = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        batch = executor.submit(client.call, 'forward_path', full22=[0.] * 22, targets=[[0.] * 16] * 100)
        try:
            assert pin.entered.wait(1)
            release = threading.Timer(.015, pin.resume.set)
            release.start()
            pose = client.call('forward_pose', full22=[0.] * 22, held16=[0.] * 16)
            assert np.allclose(pose, np.eye(4))
            assert len(batch.result(timeout=1)) == 100
            release.join(timeout=1)
        finally:
            pin.resume.set()
            server.close()


def test_fk_mapping_model_data_and_return_values_do_not_alias_solver(dispatcher):
    fwd, ik = dispatcher.forward, dispatcher.numerics
    assert fwd.arm is not ik.arm and fwd.model is not ik.arm.full_model and fwd.data is not ik.data
    assert not np.shares_memory(fwd.mapping.signs, ik.mapping.signs)
    assert not np.shares_memory(fwd.mapping.limits, ik.mapping.limits)
    assert not np.shares_memory(fwd.model.lowerPositionLimit, ik.arm.full_model.lowerPositionLimit)
    assert not np.shares_memory(fwd.arm.virtual_q, ik.arm.virtual_q)
    full, held = np.arange(22, dtype=float), np.arange(16, dtype=float)
    held[7] = 37
    poses = dispatcher.dispatch('forward_path', {'full22': full, 'targets': [held.tolist(), [0.] * 16]})
    expected = np.deg2rad(np.array([37, 14, 21]) * fwd.mapping.signs[[7, 14, 21]])
    assert np.allclose(np.array(poses[0])[:3, 3], expected)
    assert poses[0] is not poses[1] and np.array(poses[0])[0, 3] != np.array(poses[1])[0, 3]
    assert np.array_equal(full, np.arange(22)) and held[7] == 37
    assert not ik.data.q.any() and not ik.arm.virtual_q.any()


@pytest.mark.parametrize('targets', [[], [[0.] * 16] * 101, [[0.] * 15], [[0.] * 15 + [101.]],
    [[0.] * 16, [float('nan')] * 16], 'invalid'])
def test_batch_rejects_all_invalid_or_over_budget_targets_before_fk(dispatcher, targets):
    with pytest.raises(ValueError):
        dispatcher.dispatch('forward_path', {'full22': [0.] * 22, 'targets': targets})
    assert dispatcher.forward.data.calls == 0
    assert np.allclose(dispatcher.dispatch('forward_pose', {'full22': [0.] * 22, 'held16': [0.] * 16}), np.eye(4))


def test_forward_rejects_bad_body_and_forbidden_methods_and_releases_lock(dispatcher):
    with pytest.raises(ValueError):
        dispatcher.dispatch('forward_pose', {'full22': [0.] * 21, 'held16': [0.] * 16})
    with pytest.raises(ValueError, match='numerical_method_forbidden'):
        dispatcher.dispatch('release', {})
    with pytest.raises(TypeError):
        dispatcher.dispatch('forward_pose', {'full22': [0.] * 22, 'held16': [0.] * 16, 'unchecked': True})
    assert np.allclose(dispatcher.dispatch('forward_pose', {'full22': [0.] * 22, 'held16': [0.] * 16}), np.eye(4))


def test_native_collision_veto_survives_rpc_as_candidate_rejection(tmp_path, dispatcher, monkeypatch):
    from f1dagger.robot_io import CandidateSafetyRejected
    from f1_harness.owner_bridge import numerical_failure_detail

    def rejected(**kwargs):
        raise CandidateSafetyRejected('unstructured native detail', details=dict(
            collision_pair=['arm_R_Link4', 'body_Link5'], clearance_m=-.000083,
            unrelated='discard'))

    monkeypatch.setattr(dispatcher.numerics, 'solve_path', rejected)
    server = UnixServer(tmp_path / 'numerics.sock', dispatcher.dispatch).start()
    try:
        with pytest.raises(RuntimeError) as error:
            UnixClient(server.path).call('solve_path', goals=[])
        detail = numerical_failure_detail(error.value)
        assert detail['code'] == 'actual_IK_candidate_collision_rejected'
        assert detail['metrics'] == dict(collision_pair=['arm_R_Link4', 'body_Link5'],
                                         clearance_m=-.000083)
        assert 'unstructured' not in str(error.value) and 'discard' not in str(error.value)
        assert not dispatcher.solve_lock.locked()
    finally:
        server.close()


def test_unexpected_numerical_error_is_not_a_route_rejection(dispatcher, monkeypatch):
    def unexpected(**kwargs):
        raise RuntimeError('model_state_corrupt')

    monkeypatch.setattr(dispatcher.numerics, 'solve_path', unexpected)
    with pytest.raises(RuntimeError, match='^model_state_corrupt$'):
        dispatcher.dispatch('solve_path', {})
    assert not dispatcher.solve_lock.locked()
