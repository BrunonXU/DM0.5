"""The offline dependency boundary must preserve production prompt/error semantics."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_script(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('error,expected',[
    (RuntimeError('ValueError: mask_uncertain_requires_new_prompt'),ValueError),
    (RuntimeError('ValueError: camera_identity_changed'),ValueError),
    (RuntimeError('ConnectionError: incomplete IPC frame'),RuntimeError),
    (TimeoutError('timed out'),TimeoutError),
])
def test_ipc_preserves_observer_errors_without_reclassifying_transport(error,expected):
    module=load_script('replay_rpc_session')
    process=module.PerceptionProcess.__new__(module.PerceptionProcess)
    def fail(*args,**kwargs):raise error
    process.client=SimpleNamespace(call=fail)
    with pytest.raises(expected) as caught:process.call('observe',status={})
    if expected is ValueError:
        assert str(caught.value)==str(error).removeprefix('ValueError: ')
    else:assert caught.value is error


def test_perception_shares_verified_blobs_but_keeps_real_clock_events_separate(tmp_path):
    module=load_script('replay_perception_worker')
    store=module.PerceptionEvidenceStore(tmp_path/'owner',tmp_path/'perception')
    sha=store.put_blob(b'original sensor bytes')
    store.event('target_search_hint_applied',source_ns=123)
    assert store.blob(sha)==b'original sensor bytes'
    assert not (tmp_path/'owner/trace.jsonl').exists()
    row=json.loads((tmp_path/'perception/trace.jsonl').read_text())
    assert row['source_ns']==123 and row['kind']=='target_search_hint_applied'
    assert 'simulation' not in row
