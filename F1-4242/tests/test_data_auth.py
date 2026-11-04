import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import threading

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_service import DataService
from core import ControlLease
from server import App, Server


def episode(root):
    folder = root / 'all_data' / 'episode_000000'
    folder.mkdir(parents=True)
    (folder/'data_0.mcap').write_bytes(b'sample-container-for-metadata-test')
    (folder/'metadata.yaml').write_text('''rosbag2_bagfile_information:
  starting_time:
    nanoseconds_since_epoch: 1789545872522628815
  duration:
    nanoseconds: 60310191278
  message_count: 27
  topics_with_message_count:
  - topic_metadata:
      name: /hal/joint_states
      type: sensor_msgs/msg/JointState
    message_count: 27
''')
    return folder


def test_data_review_preserves_sources_and_invalidates_changed_source(tmp_path):
    root = tmp_path/'raw'
    folder = episode(root)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.name!='f1-annotations.json'}
    store = DataService(root, tmp_path/'runtime', tmp_path/'web')
    listing = store.catalog()
    assert listing['summary']['episode_count'] == 1
    row = listing['episodes'][0]
    assert row['duration_s'] == pytest.approx(60.310191278)
    assert row['topics'][0]['message_count'] == 27
    assert row['review']['status'] == 'pending'
    reviewed = store.save_review(row['id'], 'approved', '动作完成，三路画面待验收', '现场测试', row['source_signature'], row['review']['revision'])
    assert reviewed['metadata_sha256'] == hashlib.sha256((folder/'metadata.yaml').read_bytes()).hexdigest()
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.name!='f1-annotations.json'}
    assert store.episode(row['id'])['review']['status'] == 'approved'
    (folder/'data_0.mcap').write_bytes(b'changed source')
    with pytest.raises(ValueError):
        store.save_review(row['id'], 'approved', 'old view', '测试', row['source_signature'])
    changed = store.episode(row['id'], refresh=True)
    assert changed['review']['status'] == 'pending'
    assert changed['review']['stale']
    assert changed['review']['previous_review']['status'] == 'approved'


def test_data_does_not_follow_symlinks_or_accept_paths(tmp_path):
    root = tmp_path/'raw'
    folder = episode(root)
    secret = tmp_path/'outside'
    secret.mkdir()
    (secret/'metadata.yaml').write_text('private')
    (secret/'hidden.mcap').write_text('private')
    (root/'foreign').symlink_to(secret, target_is_directory=True)
    (folder/'secret.txt').symlink_to(secret/'hidden.mcap')
    store = DataService(root, tmp_path/'runtime', tmp_path/'web')
    rows = store.catalog()['episodes']
    assert len(rows) == 1
    assert all(item['name'] != 'secret.txt' for item in rows[0]['files'])
    with pytest.raises(ValueError):
        store.episode('../outside')
    with pytest.raises(ValueError):
        DataService(root, root/'reviews', tmp_path/'web')
    linked = tmp_path/'linked'
    linked.symlink_to(root, target_is_directory=True)
    assert not DataService(linked, tmp_path/'runtime2', tmp_path/'web').catalog()['available']


def test_recording_observation_requires_real_bag_command_and_source(tmp_path):
    root = tmp_path/'raw'; root.mkdir()
    proc = tmp_path/'proc'; proc.mkdir()
    p = proc/'123'; p.mkdir()
    (p/'cmdline').write_bytes(b'python\0/home/user/lxl/all_remote_lxl.py\0')
    store = DataService(root, tmp_path/'runtime', tmp_path/'web', proc)
    assert store.recording()['state'] == 'no_process_observed'
    (p/'cmdline').write_bytes(b'/usr/bin/python3\0/opt/ros/jazzy/bin/ros2\0bag\0record\0-o\0' + str(root/'episode').encode() + b'\0')
    assert store.recording()['state'] == 'observed_active'
    assert not store.recording()['control_enabled']
    missing = DataService(root, tmp_path/'runtime2', tmp_path/'web', tmp_path/'absent-proc')
    assert missing.recording()['state'] == 'unknown'


@pytest.fixture
def http_app(tmp_path):
    root = tmp_path/'raw'; episode(root)
    profile = json.loads((Path(__file__).resolve().parents[1]/'configs/f1-09.json').read_text())
    profile['data_path'] = str(root)
    runtime = tmp_path/'runtime'
    app = App(profile, runtime, tmp_path/'control.lock', offline=True)
    server = Server(('127.0.0.1', 0), app, [])
    host = f'127.0.0.1:{server.server_address[1]}'
    server.allowed.add(host)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def request(path, body=None, cookie=None, origin=True):
        connection = http.client.HTTPConnection(host, timeout=10)
        headers = {'Host': host}
        if cookie: headers['Cookie'] = cookie
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'X-F1-Request': '1'})
            if origin: headers['Origin'] = 'http://' + host
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        status = response.status; cookie = response.getheader('Set-Cookie')
        data = json.loads(response.read()); connection.close()
        return status, data, cookie.split(';')[0] if cookie else None
    yield app, request
    server.shutdown(); server.server_close(); app.close(); thread.join(2)


def test_passwordless_acquisition_preserves_session_mutex_csrf_and_no_motion(http_app):
    app, request = http_app
    status, session, cookie = request('/api/session')
    assert status == 200 and session['authenticated'] and not session['password_required']
    assert not (app.runtime/'access.token').exists()
    assert not (app.runtime/'control_code.json').exists()
    assert app.lease.fd is None and app.actions.latest() is None
    assert request('/api/control/acquire', {}, cookie, origin=False)[0] == 403
    assert request('/api/login', {}, cookie)[0] == 200
    assert app.lease.fd is None
    external = ControlLease(app.lease.path)
    external.acquire('pico-test')
    try:
        assert request('/api/control/acquire', {}, cookie)[0] == 409
    finally:
        external.release()
    status, acquired, cookie = request('/api/control/acquire', {'owner':'操作员'}, cookie)
    assert status == 200 and acquired['authenticated']
    assert app.actions.latest() is None
    # Neither another browser nor a guessed lease can steal control.
    assert request('/api/control/acquire', {})[0] == 409
    assert request('/api/control/heartbeat', {'lease_id':acquired['lease_id']})[0] == 409
    assert request('/api/control/heartbeat', {'lease_id':'wrong'}, cookie)[0] == 409
    assert request('/api/control/release', {'lease_id':acquired['lease_id']}, cookie)[0] == 200


def test_old_credential_files_are_not_read_or_rewritten(http_app):
    app, request = http_app
    for name in ('control_code.json','access.token'):
        (app.runtime/name).write_text('invalid legacy content')
    _, _, cookie = request('/api/session')
    assert request('/api/control/acquire', {}, cookie)[0] == 200
    assert app.actions.latest() is None
    for name in ('control_code.json','access.token'):
        assert (app.runtime/name).read_text() == 'invalid legacy content'


def test_http_review_requires_origin_but_no_password_or_motion_lease(http_app):
    app, request = http_app
    _, catalog, cookie = request('/api/data')
    eid = catalog['episodes'][0]['id']
    assert request('/api/data/episodes/'+eid, cookie=cookie)[0] == 200
    body = {'episode_id':eid,'status':'rejected','notes':'需要复核','expected_source_signature':catalog['episodes'][0]['source_signature'],'expected_review_revision':catalog['episodes'][0]['review']['revision']}
    assert request('/api/data/reviews', body, cookie, origin=False)[0] == 403
    assert request('/api/login', {}, cookie)[0] == 200
    assert request('/api/data/reviews', body, cookie)[0] == 200
    assert app.lease.fd is None
    assert app.actions.latest() is None
    assert request('/api/data/recording', cookie=cookie)[1]['recording']['control_enabled'] is False
    assert request('/api/data/recording/start', {}, cookie)[0] == 404


def test_revision_does_not_read_mesh_content(http_app, monkeypatch):
    app, _ = http_app
    original = Path.read_bytes
    def guard(path):
        if path.suffix in ('.stl','.glb','.gltf','.dae'):
            raise AssertionError('revision must not read mesh payloads')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', guard)
    assert len(app.revision()) == 16


def test_revision_uses_published_batch_marker(http_app, tmp_path):
    app, _ = http_app
    app.web_root = tmp_path/'web'; app.web_root.mkdir()
    (tmp_path/'sync.json').write_text('{"files":{"a.js":"first"}}')
    first = app.revision()
    (app.web_root/'a.js').write_text('partial next batch')
    assert app.revision() == first
    (tmp_path/'sync.json').write_text('{"files":{"a.js":"second"}}')
    assert app.revision() != first


def test_static_json_is_served_without_double_serialization(http_app, tmp_path):
    app, request = http_app
    app.web_root = tmp_path/'web'; app.web_root.mkdir()
    (app.web_root/'manifest.json').write_text('{"schema":"model","links":[]}')
    status, data, _ = request('/manifest.json')
    assert status == 200 and data == {'schema':'model','links':[]}
