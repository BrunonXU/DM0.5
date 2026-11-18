import json
from pathlib import Path

import pytest

from f1_harness.control import build_bridge
from f1_harness.ipc import UnixClient


def config(tmp_path):
    source=Path(__file__).resolve().parents[1]/'configs/owner.example.json'
    value=json.loads(source.read_text())
    for key in ('owner_socket','planner_socket','verifier_socket'):
        value[key]=str(tmp_path/(key+'.sock'))
    path=tmp_path/'owner.json'
    path.write_text(json.dumps(value))
    return path


def test_shadow_starts_without_invented_measured_stop_thresholds(tmp_path):
    path=config(tmp_path)
    bridge=build_bridge(path,tmp_path/'run',powered=False)
    try:
        socket=Path(json.loads(path.read_text())['owner_socket'])
        status=UnixClient(socket).call('status')
        assert status['mode']=='shadow'
        assert not status['authorized'] and not status['hardware_ready']
        assert status['recording_identity']['owner_run_id']=='run'
        assert status['perception_context']['phase']=='observe'
    finally:
        bridge.close();bridge.journal.close()


def test_hardware_rejects_unmeasured_fields_before_creating_journal(tmp_path):
    path=config(tmp_path)
    with pytest.raises(ValueError,match='positive_measured'):
        build_bridge(path,tmp_path/'run',powered=True)
    assert not (tmp_path/'run').exists()


def test_bad_optional_shadow_threshold_does_not_get_defaulted(tmp_path):
    path=config(tmp_path);value=json.loads(path.read_text());value['stop_delta_deg']=0
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='positive_measured'):
        build_bridge(path,tmp_path/'run',powered=False)
    assert not (tmp_path/'run').exists()
