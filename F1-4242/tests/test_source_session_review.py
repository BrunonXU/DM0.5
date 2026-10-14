"""Independent local-Pico provenance tests. No ROS node or subprocess starts."""
import copy
import fcntl
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recording.source_session import EXPECTED_NAMES, PicoSessionReader, PicoSourceEpoch

T0 = 10_000_000_000


def graph():
    return {kind: [{'name': name, 'gid': bytes([i]) * 16}]
            for i, (kind, name) in enumerate(EXPECTED_NAMES.items(), 1)}


def epoch_fixture():
    identity = {'supervisor': {'pid': 100, 'start': '123'},
                'children': [{'pid': 101, 'start': '124'}, {'pid': 102, 'start': '125'}],
                'source_root': '/verified/pico', 'session_started_at': 900.0}
    reader = NS(read=lambda: copy.deepcopy(identity))
    epoch = PicoSourceEpoch(reader)
    assert not epoch.update(graph(), T0, 10.0)
    assert not epoch.update(graph(), T0 + 900_000_000, 10.9)
    assert epoch.update(graph(), T0 + 1_000_000_000, 11.0)
    return epoch, identity


def test_stable_proof_admits_fresh_receipt_and_names_its_actual_basis():
    epoch, _ = epoch_fixture()
    result = epoch.admit('left', {'received_timestamp': T0 + 1_005_000_000},
                         T0 + 1_010_000_000, 11.01)
    assert result['stamp_ns'] == T0 + 1_005_000_000
    assert result['source_basis'] == 'verified_pico_session_and_graph'
    assert result['observed_graph_gid'] == graph()['left'][0]['gid'].hex()
    assert result['session_identity'] == epoch.signature
    assert result['epoch'] == epoch.epoch


@pytest.mark.parametrize('stamp', [None, 0, True, 11_000_000_000.0,
                                  T0 + 999_999_999, T0 + 1_020_000_000])
def test_missing_noninteger_old_epoch_or_future_receipts_are_rejected(stamp):
    epoch, _ = epoch_fixture()
    with pytest.raises(ValueError):
        epoch.admit('left', {'received_timestamp': stamp}, T0 + 1_010_000_000, 11.01)


def test_old_queued_command_cannot_be_initially_admitted_as_a_hold():
    epoch, _ = epoch_fixture()
    with pytest.raises(ValueError):
        epoch.admit('left', {'received_timestamp': T0 + 1_001_000_000},
                    T0 + 1_052_000_000, 11.052)


def test_proof_expiration_and_non_dict_receipts_are_rejected():
    epoch, _ = epoch_fixture()
    with pytest.raises(ValueError):
        epoch.admit('left', {'received_timestamp': T0 + 1_501_000_000},
                    T0 + 1_501_000_000, 11.501)
    with pytest.raises(ValueError):
        epoch.admit('left', NS(received_timestamp=T0 + 1_001_000_000),
                    T0 + 1_001_000_000, 11.001)


@pytest.mark.parametrize('fault', ['missing', 'multiple', 'wrong_name', 'zero_gid', 'new_gid'])
def test_writer_boundary_invalidates_previously_stable_source(fault):
    epoch, _ = epoch_fixture()
    previous = epoch.epoch
    changed = graph()
    if fault == 'missing': changed['left'] = []
    if fault == 'multiple': changed['left'] *= 2
    if fault == 'wrong_name': changed['left'][0]['name'] = 'another_teleop'
    if fault == 'zero_gid': changed['left'][0]['gid'] = bytes(16)
    if fault == 'new_gid': changed['left'][0]['gid'] = bytes([9]) * 16
    assert not epoch.update(changed, T0 + 1_020_000_000, 11.02)
    assert epoch.epoch > previous
    with pytest.raises(ValueError):
        epoch.admit('left', {'received_timestamp': T0 + 1_021_000_000},
                    T0 + 1_021_000_000, 11.021)


def test_reused_child_pid_changes_identity_and_requires_stability_again():
    epoch, identity = epoch_fixture()
    old_signature, old_epoch = epoch.signature, epoch.epoch
    identity['children'][0]['start'] = '999'
    assert not epoch.update(graph(), T0 + 1_020_000_000, 11.02)
    assert epoch.signature != old_signature and epoch.epoch > old_epoch
    assert not epoch.update(graph(), T0 + 1_900_000_000, 11.9)


def test_session_loss_then_same_graph_return_requires_new_epoch_and_receipt():
    epoch, identity = epoch_fixture()
    old_epoch = epoch.epoch
    epoch.reader.read = lambda: (_ for _ in ()).throw(ValueError('lock released'))
    assert not epoch.update(graph(), T0 + 1_100_000_000, 11.1)
    epoch.reader.read = lambda: copy.deepcopy(identity)
    assert not epoch.update(graph(), T0 + 1_200_000_000, 11.2)
    assert epoch.update(graph(), T0 + 2_300_000_000, 12.3)
    assert epoch.epoch > old_epoch
    with pytest.raises(ValueError):
        epoch.admit('left', {'received_timestamp': T0 + 1_001_000_000},
                    T0 + 2_301_000_000, 12.301)


@pytest.mark.parametrize('where', ['graph', 'receipt'])
def test_ros_clock_rollback_invalidates_source(where):
    epoch, _ = epoch_fixture()
    if where == 'graph':
        assert not epoch.update(graph(), T0, 11.02)
    else:
        with pytest.raises(ValueError):
            epoch.admit('left', {'received_timestamp': T0}, T0, 11.02)
    assert epoch.valid is False


@pytest.fixture
def proc_fixture(tmp_path):
    root = tmp_path / 'pico'; root.mkdir()
    proc = tmp_path / 'proc'; proc.mkdir()
    lock = tmp_path / 'control.lock'
    state = {'owner': 'pico_upper', 'pid': 101, 'process_start': '111',
             'mode': 'arms_and_grippers', 'source_root': str(root), 'started_at': 99.5}
    lock.write_text(json.dumps(state))
    session = tmp_path / 'session.json'; session.write_text(json.dumps(state))

    def process(pid, module, ppid, start):
        path = proc / str(pid); path.mkdir(exist_ok=True)
        parts = ['0'] * 20; parts[0] = 'S'; parts[1] = str(ppid); parts[19] = str(start)
        (path / 'stat').write_text(str(pid) + ' (python) ' + ' '.join(parts))
        args = ['python', '-m', module] + (['start'] if module.endswith('supervisor') else [])
        (path / 'cmdline').write_bytes(b'\0'.join(s.encode() for s in args) + b'\0')
        (path / 'cwd').symlink_to(root, target_is_directory=True)
        (path / 'fd').mkdir(); (path / 'fd/3').symlink_to(lock)

    process(101, 'pico_upper.supervisor', 1, 111)
    process(202, 'pico_upper.bridge', 101, 222)
    process(203, 'pico_upper.teleop', 101, 333)
    children = proc / '101/task/101/children'; children.parent.mkdir(parents=True)
    children.write_text('202 203')
    handle = lock.open('r+')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reader = PicoSessionReader(session, lock, root, proc)
    yield NS(reader=reader, proc=proc, root=root, state=state, session=session,
             lock=lock, handle=handle, children=children, process=process)
    handle.close()


def test_complete_process_tree_and_shared_held_lock_prove_local_session(proc_fixture):
    fixture = proc_fixture
    proof = fixture.reader.read()
    assert proof['supervisor']['pid'] == 101
    assert [child['pid'] for child in proof['children']] == [202, 203]
    assert [child['start'] for child in proof['children']] == ['222', '333']
    assert proof['source_root'] == str(fixture.root)


@pytest.mark.parametrize('fault', ['unheld_lock', 'owner_mismatch', 'pid_reuse', 'missing_child',
                                 'wrong_parent', 'duplicate_child', 'wrong_cwd', 'no_lock_fd',
                                 'module_argument_spoof', 'different_source'])
def test_incomplete_or_conflicting_process_proof_is_rejected(proc_fixture, fault):
    f = proc_fixture
    if fault == 'unheld_lock': fcntl.flock(f.handle, fcntl.LOCK_UN)
    if fault == 'owner_mismatch': f.lock.write_text(json.dumps(dict(f.state, pid=909)))
    if fault == 'pid_reuse': f.session.write_text(json.dumps(dict(f.state, process_start='999')))
    if fault == 'missing_child': f.children.write_text('202')
    if fault == 'wrong_parent':
        p = f.proc / '202/stat'; p.write_text(p.read_text().replace('S 101 ', 'S 909 '))
    if fault == 'duplicate_child':
        f.process(204, 'pico_upper.bridge', 101, 444); f.children.write_text('202 203 204')
    if fault == 'wrong_cwd':
        p = f.proc / '203/cwd'; p.unlink(); p.symlink_to(f.root.parent)
    if fault == 'no_lock_fd': (f.proc / '203/fd/3').unlink()
    if fault == 'module_argument_spoof':
        (f.proc / '202/cmdline').write_bytes(b'python\0unrelated.py\0pico_upper.bridge\0')
    if fault == 'different_source': f.session.write_text(json.dumps(dict(f.state, source_root=str(f.root.parent))))
    with pytest.raises(ValueError): f.reader.read()


@pytest.mark.parametrize('file_kind', ['session', 'lock'])
@pytest.mark.parametrize('value', [[], None, 'invalid-object', 7])
def test_nonobject_session_or_lock_json_fails_closed(proc_fixture, file_kind, value):
    fixture = proc_fixture
    path = fixture.session if file_kind == 'session' else fixture.lock
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        fixture.reader.read()
