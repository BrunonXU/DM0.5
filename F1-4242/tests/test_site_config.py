"""Names and network addresses do not rewrite a robot's control identity."""
import hashlib
import json
from pathlib import Path

import pytest

from project_paths import console_allowed_hosts, installation_environment, read_profile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    profile = tmp_path/'robot.json'
    profile.write_bytes((ROOT/'configs/robot.example.json').read_bytes())
    site = tmp_path/'site.json'
    site.write_text(json.dumps(dict(schema='f1_site_v1', label='实验室 F1', ip='192.0.2.16')))
    monkeypatch.setenv('F1_SITE_PATH', str(site))
    return profile, site


def test_rename_and_network_change_preserve_control_profile(configuration):
    profile, site = configuration
    original = profile.read_bytes()
    before = json.loads(original)
    current = read_profile(profile)
    assert current['label'] == '实验室 F1'
    assert current['ip'] == '192.0.2.16'
    for key in ('id', 'robot_id', 'expected_machine_id', 'limits', 'motion_enabled'):
        assert current[key] == before[key]
    site.write_text(json.dumps(dict(schema='f1_site_v1', label='改个名字', ip='192.0.2.17')))
    renamed = read_profile(profile)
    assert renamed['label'] == '改个名字' and renamed['ip'] == '192.0.2.17'
    assert hashlib.sha256(profile.read_bytes()).digest() == hashlib.sha256(original).digest()
    assert profile.read_bytes() == original


@pytest.mark.parametrize('updates', [
    {'motion_enabled': True}, {'id': 'another-machine'}, {'label': ''},
    {'label': 'new\nname'}, {'ip': '127.0.0.1:4242'}, {'ip': '0.0.0.0'},
    {'ip': '224.0.0.1'}, {'schema': 'unknown'},
])
def test_site_cannot_override_control_or_supply_invalid_values(configuration, updates):
    profile, site = configuration
    value = json.loads(site.read_text())
    value.update(updates)
    site.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        read_profile(profile)


def test_missing_site_fails_instead_of_silently_using_old_ip(configuration):
    profile, site = configuration
    site.unlink()
    with pytest.raises(ValueError):
        read_profile(profile)


def test_legacy_profile_without_site_is_unchanged(configuration, monkeypatch):
    profile, _ = configuration
    monkeypatch.delenv('F1_SITE_PATH')
    actual = read_profile(profile)
    original = json.loads(profile.read_text())
    assert actual['label'] == original['label'] and actual['ip'] == original['ip']


def test_site_path_is_propagated_to_child_installations(configuration):
    _, site = configuration
    assert installation_environment()['F1_SITE_PATH'] == str(site)


def test_wired_primary_wifi_backup_preserve_control_identity(configuration):
    profile, site = configuration
    original = profile.read_bytes()
    site.write_text(json.dumps(dict(schema='f1_site_v1', label='F1-09',
                                   ip='192.0.2.7', backup_ips=['192.0.2.3'])))
    actual = read_profile(profile)
    assert actual['ip'] == '192.0.2.7'
    assert actual['backup_ips'] == ['192.0.2.3']
    assert console_allowed_hosts(actual, 4242) == {
        '127.0.0.1:4242', 'localhost:4242', '192.0.2.7:4242', '192.0.2.3:4242'}
    assert profile.read_bytes() == original


@pytest.mark.parametrize('backups', [
    '192.0.2.3', [None], ['0.0.0.0'], ['224.0.0.1'], ['255.255.255.255'],
    ['192.0.2.3:4242'], ['https://example.com'], ['192.0.2.16'],
    ['192.0.2.3', '192.0.2.3'], ['10.0.0.' + str(i) for i in range(1, 6)],
])
def test_invalid_or_duplicate_backup_addresses_are_rejected(configuration, backups):
    profile, site = configuration
    value = json.loads(site.read_text())
    value['backup_ips'] = backups
    site.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        read_profile(profile)


def test_primary_and_backup_http_keep_host_and_origin_checks(configuration):
    import http.client
    import threading
    from types import SimpleNamespace
    from server import Server

    profile, site = configuration
    site.write_text(json.dumps(dict(schema='f1_site_v1', label='F1-09',
                                   ip='192.0.2.7', backup_ips=['192.0.2.3'])))
    app = SimpleNamespace(session=lambda cookie: ('test-session', {}))
    server = Server(('127.0.0.1', 0), app, [])
    port = server.server_address[1]
    server.allowed = console_allowed_hosts(read_profile(profile), port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(host, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        headers = {'Host': host}
        if origin is not None:
            headers.update(Origin=origin, **{'X-F1-Request': '1'})
            connection.request('POST', '/api/login', '{}', headers)
        else:
            connection.request('GET', '/api/session', headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status
    try:
        wired = f'192.0.2.7:{port}'
        wifi = f'192.0.2.3:{port}'
        for host in (wired, wifi):
            assert request(host) == 200
            assert request(host, 'http://' + host) == 200
        assert request(f'192.0.2.18:{port}') == 403
        assert request(wifi, 'http://' + wired) == 403
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
