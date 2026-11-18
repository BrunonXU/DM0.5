"""Startup concurrency, planner-only networking, and visible failure causes."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from f1_harness.web_execution import WorkspaceSupervisor
from f1_harness.web_process import WorkspaceProcesses


@pytest.mark.parametrize('name,network', [('planner', True), ('owner', False), ('robot_services', False)])
def test_proxy_is_only_passed_to_planner(tmp_path, monkeypatch, name, network):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('SSL_CERT_FILE', '/tmp/test-ca.pem')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-must-not-be-forwarded')
    seen = {}
    def popen(argv, **kwargs):
        seen.update(kwargs)
        return object()
    monkeypatch.setattr('f1_harness.web_process.subprocess.Popen', popen)
    processes = WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.run, processes.children = tmp_path, []
    processes.root = tmp_path/'frozen-strategy'
    processes.config = {}
    processes.config={"_model_pool_socket":"/tmp/private-model-pool.sock",
                      "_subscription_credentials":"/private/subscription.json"}
    processes.spawn(dict(name=name, argv=['unused']))
    assert ('F1_MODEL_POOL_SOCKET' in seen['env']) is network
    assert ('F1_CHATGPT_SUBSCRIPTION_CREDENTIALS' in seen['env']) is network
    assert ('HTTPS_PROXY' in seen['env']) == network
    assert ('SSL_CERT_FILE' in seen['env']) == network
    assert 'OPENAI_API_KEY' not in seen['env']
    if network:
        assert {'127.0.0.1', 'localhost', '::1'} <= set(seen['env']['no_proxy'].split(','))
    for _, _, log in processes.children:
        log.close()


def test_auxiliaries_are_all_spawned_before_waiting_for_dependencies(tmp_path):
    processes = WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.run = tmp_path
    processes.rows = [dict(name=name) for name in ('control_tunnel', 'image_tunnel', 'robot_services')]
    started = []
    processes.spawn = lambda row: started.append(row['name'])
    processes.check = lambda: None
    def ready(row, path):
        assert started == [r['name'] for r in processes.rows]
        return True
    processes.helper = SimpleNamespace(required_ports=lambda rows: [], require_free_ports=lambda ports: None, ready=ready)
    processes.start_services(threading.Event())


def test_ready_logs_do_not_hide_exited_auxiliary(tmp_path):
    processes = WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.run, processes.rows = tmp_path, [dict(name='robot_services')]
    processes.spawn = lambda row: None
    def failed():
        raise RuntimeError('auxiliary exited after ready')
    processes.check = failed
    processes.helper = SimpleNamespace(required_ports=lambda rows: [], require_free_ports=lambda ports: None,
                                      ready=lambda row, path: True)
    with pytest.raises(RuntimeError, match='exited after ready'):
        processes.start_services(threading.Event())


def test_cancelled_start_does_not_spawn_auxiliary(tmp_path):
    processes = WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.run, processes.rows = tmp_path, [dict(name='robot_services')]
    processes.helper = SimpleNamespace(required_ports=lambda rows: [], require_free_ports=lambda ports: None)
    processes.spawn = lambda row: pytest.fail('cancelled startup spawned process')
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        processes.start_services(stop)


@pytest.mark.parametrize('code,expected', [
    ('model_connect_error', '模型服务连接失败'),
    ('model_read_timeout', '模型响应等待失败'),
    ('model_http_401', '任务规划失败'),
])
def test_web_surfaces_planner_failure_file_without_result(tmp_path, code, expected):
    (tmp_path / 'planner').mkdir()
    (tmp_path / 'planner/failure.json').write_text(json.dumps(dict(type='RuntimeError', code=code)))
    status, summary, attention = WorkspaceSupervisor._planner_outcome(tmp_path, 1)
    assert status == 'error' and expected in summary and code in summary
    assert attention is None


def test_owner_initialization_overlaps_services_without_granting_start(tmp_path):
    processes=WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.run=tmp_path;processes.rows=[dict(name='services')]
    calls=[];processes.spawn=lambda row:calls.append('spawn')
    processes.check=lambda:None
    def ready(row,path):
        assert calls==['spawn','owner_initialize']
        return True
    processes.helper=SimpleNamespace(required_ports=lambda rows:[],require_free_ports=lambda ports:None,ready=ready)
    processes.start_services_with_owner(threading.Event(),lambda:calls.append('owner_initialize'))
    assert calls==['spawn','owner_initialize']
    stop=threading.Event();stop.set()
    with pytest.raises(InterruptedError):processes.start_services_with_owner(stop,lambda:pytest.fail('cancelled owner'))
