"""Drivers may arrive after the HTTP service without causing automatic motion."""
from pathlib import Path
from types import SimpleNamespace
import copy,sys,threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App

def fixture(enabled=True):
    app=App.__new__(App)
    app.requested_profile={'motion_enabled':enabled}
    app.profile={'motion_enabled':False}
    app.adapter=SimpleNamespace(profile={'motion_enabled':False})
    app.op_lock=threading.RLock();app.closed=threading.Event()
    app.lease=SimpleNamespace(fd=None)
    app.actions=SimpleNamespace(busy=lambda:False)
    app.contract={'ok':False,'errors':['motor not started']}
    events=[];app.events=SimpleNamespace(add=lambda *a,**kw:events.append((a,kw)))
    app.contract_verifier=lambda p:{'ok':True,'errors':[]}
    return app,events

def test_late_drivers_restore_only_configured_capability():
    app,events=fixture()
    assert app._retry_contract_once()
    assert app.contract['ok'] and app.profile['motion_enabled'] and app.adapter.profile['motion_enabled']
    assert len(events)==1 and app.lease.fd is None  # Fake adapter has no action or publisher methods.
    assert app._retry_contract_once() and len(events)==1

def test_explicit_read_only_profile_stays_read_only_after_verification():
    app,_=fixture(False)
    assert app._retry_contract_once() and app.contract['ok']
    assert not app.profile['motion_enabled'] and not app.adapter.profile['motion_enabled']

def test_missing_or_mismatched_driver_keeps_retrying_read_only():
    app,events=fixture()
    app.contract_verifier=lambda p:{'ok':False,'errors':['binary differs']}
    assert not app._retry_contract_once()
    assert not app.profile['motion_enabled'] and not events

def test_held_lease_defers_check():
    app,_=fixture();app.lease.fd=object()
    def forbidden(_):raise AssertionError('must not run during lease')
    app.contract_verifier=forbidden
    assert not app._retry_contract_once() and not app.profile['motion_enabled']

def test_lease_acquired_during_parameter_check_defers_enabling():
    app,_=fixture()
    def verify(_):
        app.lease.fd=object()
        return {'ok':True,'errors':[]}
    app.contract_verifier=verify
    assert not app._retry_contract_once() and not app.contract['ok']
    assert not app.adapter.profile['motion_enabled']

def test_shutdown_during_check_cannot_enable():
    app,events=fixture()
    def verify(_):
        app.closed.set()
        return {'ok':True,'errors':[]}
    app.contract_verifier=verify
    assert app._retry_contract_once() and not app.profile['motion_enabled'] and not events

def test_exception_is_visible_and_retryable():
    app,events=fixture()
    def unavailable(_):raise RuntimeError('DDS timeout')
    app.contract_verifier=unavailable
    assert not app._retry_contract_once()
    assert 'DDS timeout' in app.contract['errors'] and not events

from core import OfflineAdapter
import pytest

class ReconnectingAdapter:
    def __init__(self,profile):self.profile=copy.deepcopy(profile);self.started=False;self.closed=False
    def start(self):
        assert not self.profile['motion_enabled']
        self.started=True
    def close(self):self.closed=True


def offline_fixture():
    app,events=fixture();app.adapter=OfflineAdapter('network unavailable');app.actions.adapter=app.adapter
    made=[]
    def factory(profile):
        candidate=ReconnectingAdapter(profile);made.append(candidate);return candidate
    app.adapter_factory=factory
    return app,events,made


def test_failed_ros_initialization_recovers_subscriptions_and_action_reference():
    app,events,made=offline_fixture()
    assert app._retry_contract_once()
    assert app.adapter is made[0] and app.actions.adapter is made[0]
    assert made[0].started and made[0].profile['motion_enabled'] and not made[0].closed
    assert app.lease.fd is None and len(events)==2


def test_dds_unavailable_again_keeps_retrying_even_if_contract_ok():
    app,events,made=offline_fixture()
    normal=app.adapter_factory
    def failing(profile):
        value=normal(profile)
        def start():raise RuntimeError('interface missing')
        value.start=start;return value
    app.adapter_factory=failing
    assert not app._retry_contract_once() and app.contract['ok']
    assert isinstance(app.adapter,OfflineAdapter) and made[0].closed
    app.adapter_factory=normal
    assert app._retry_contract_once() and len(made)==2


def test_reconnect_without_driver_contract_restores_only_feedback():
    app,events,made=offline_fixture();app.contract_verifier=lambda p:{'ok':False,'errors':['driver missing']}
    assert not app._retry_contract_once()
    assert app.adapter is made[0] and not app.adapter.profile['motion_enabled']
    app.contract_verifier=lambda p:{'ok':True,'errors':[]}
    assert app._retry_contract_once() and len(made)==1


@pytest.mark.parametrize('interrupt',['lease','busy','shutdown'])
def test_recovery_race_discards_subscriptions_without_switching_action_owner(interrupt):
    app,events,made=offline_fixture();old=app.adapter;normal=app.adapter_factory
    def factory(profile):
        candidate=normal(profile)
        if interrupt=='lease':app.lease.fd=object()
        elif interrupt=='busy':app.actions.busy=lambda:True
        else:app.closed.set()
        return candidate
    app.adapter_factory=factory
    app._retry_contract_once()
    assert app.adapter is old and app.actions.adapter is old and made[0].closed and not events


def test_real_app_initial_node_failure_schedules_recovery(tmp_path,monkeypatch):
    import json,robot.adapter,robot.runtime_contract
    profile=json.loads((Path(__file__).resolve().parents[1]/'configs/f1-09.json').read_text())
    class Fails(ReconnectingAdapter):
        def start(self):raise RuntimeError('network interface not ready')
    monkeypatch.setattr(robot.adapter,'RosAdapter',Fails)
    monkeypatch.setattr(robot.runtime_contract,'verify_runtime_contract',lambda p:{'ok':False,'errors':['driver missing']})
    app=App(profile,tmp_path/'runtime',tmp_path/'control.lock')
    try:
        assert isinstance(app.adapter,OfflineAdapter)
        assert app.contract_thread is not None and app.contract_thread.is_alive()
        assert app.lease.fd is None and not app.actions.busy()
    finally:app.close()
