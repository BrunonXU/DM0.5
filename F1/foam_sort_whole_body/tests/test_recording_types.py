from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay import recording_check as rc
from f1_replay.runtime import ReplayError


def fake_types(monkeypatch, lib):
    module=ModuleType('ament_index_python.packages');module.get_package_prefix=lambda p:'/verified/'+p
    monkeypatch.setitem(sys.modules,'ament_index_python.packages',module)
    from f1_replay import cli
    monkeypatch.setattr(cli,'select_recording_topics',lambda *a:(['/driver/State'],{}))
    monkeypatch.setattr(rc.ctypes,'CDLL',lambda *a:lib)
    backend=SimpleNamespace(topic_peers=lambda *a:{'/driver/State':{'publishers':[{'type':'driver/msg/State'}]}})
    return { 'topics':{key:'/'+key for key in rc.COMMAND_TYPES}},backend


def test_existing_library_with_missing_state_symbol_fails_before_recorder(monkeypatch):
    profile,backend=fake_types(monkeypatch,object())
    with pytest.raises(ReplayError,match='driver/msg/State'):
        rc.check_types(profile,backend)


def test_exact_symbol_and_selected_package_are_reported(monkeypatch):
    lib=SimpleNamespace(rosidl_typesupport_cpp__get_message_type_support_handle__driver__msg__State=object())
    profile,backend=fake_types(monkeypatch,lib)
    result=rc.check_types(profile,backend)
    assert result['types']['/driver/State']['library'].startswith('/verified/driver/')
    assert result['recorder_started'] is False
