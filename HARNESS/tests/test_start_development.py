import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('start_development',ROOT/'scripts/start_development.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


def settings(tmp_path):
    site=tmp_path/'local site';site.mkdir()
    (site/'gateway.local.json').write_text(json.dumps(dict(control_port=19840,image_port=19841)))
    (site/'masks.local.json').write_text(json.dumps(dict(port=19842)))
    return SimpleNamespace(local_site=str(site),remote_site='/home/user/Bruno/HARNESS/site/test',
        remote_bundle='/home/user/Bruno/HARNESS/development/frozen',host='user@127.0.0.1',
        port=22009,identity=str(tmp_path/'key'),known_hosts=str(tmp_path/'known_hosts'))


def test_launcher_uses_separate_tunnels_original_services_and_no_owner(tmp_path):
    args=settings(tmp_path)
    rows,instructions=launcher.commands(args,ROOT,tmp_path/'launcher run')
    assert [row['name'] for row in rows]==['masks','control_tunnel','image_tunnel','robot_services']
    assert '-N' in rows[1]['argv'] and '-N' in rows[2]['argv']
    assert '127.0.0.1:19840:127.0.0.1:19840' in rows[1]['argv']
    assert '127.0.0.1:19841:127.0.0.1:19841' in rows[2]['argv']
    assert '127.0.0.1:19842:127.0.0.1:19842' in rows[2]['argv']
    remote=shlex.split(rows[3]['argv'][-1])
    assert remote[:2]==['python3','-c'] and remote[2]==launcher.REMOTE_GUARD
    assert 'services' in shlex.split(remote[-1].split('\nexec ',1)[1])
    assert 'owner' not in shlex.split(remote[-1].split('\nexec ',1)[1])
    assert '--planner astra' in instructions['planner_command']
    assert '--image-gateway http://127.0.0.1:19841' in instructions['planner_command']
    assert 'owner' in instructions['owner_command'] and '--mode hardware' in instructions['owner_command']


def test_launcher_refuses_wrong_paths_and_ports(tmp_path):
    args=settings(tmp_path);args.remote_site='/home/user/Bruno/HARNESS/site/../../production'
    with pytest.raises(ValueError,match='remote_path'):
        launcher.commands(args,ROOT,tmp_path/'run')
    args.remote_site='/home/user/Bruno/HARNESS/site/test'
    (Path(args.local_site)/'masks.local.json').write_text('{"port": 9999}')
    with pytest.raises(ValueError,match='registered'):
        launcher.commands(args,ROOT,tmp_path/'run')


def test_local_color_uses_registered_native_masks_without_image_roundtrip(tmp_path):
    args=settings(tmp_path)
    path=Path(args.local_site)/'gateway.local.json'
    config=json.loads(path.read_text());config['scene']={'mask_execution':'local_fixed_color'}
    path.write_text(json.dumps(config))
    rows,_=launcher.commands(args,ROOT,tmp_path/'run')
    assert [row['name'] for row in rows]==['control_tunnel','image_tunnel','robot_services']
    assert '-R' not in rows[1]['argv']
    assert launcher.required_ports(rows)==(19840,19841)


def test_port_occupancy_is_rejected_without_killing_anything(monkeypatch):
    objects=[]
    class Occupied:
        closed=False
        def setsockopt(self,*args):pass
        def listen(self,*args):pass
        def bind(self,address):
            if address[1]==19841:raise OSError('already used')
        def close(self):self.closed=True
    def make():
        item=Occupied();objects.append(item);return item
    monkeypatch.setattr(launcher.socket,'socket',make)
    with pytest.raises(ValueError,match='19841'):
        launcher.require_free_ports()
    assert all(item.closed for item in objects)


def test_closed_tunnel_time_wait_does_not_prevent_restart():
    import socket
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind(('127.0.0.1',0));server.listen(1)
        port=server.getsockname()[1]
        with socket.create_connection(('127.0.0.1',port),timeout=1) as client:
            peer,_=server.accept()
            # Server actively closes, leaving its local port in TIME_WAIT.
            peer.shutdown(socket.SHUT_WR)
            assert client.recv(1)==b''
            client.shutdown(socket.SHUT_WR)
            assert peer.recv(1)==b''
            peer.close()
    with socket.socket() as old_probe:
        with pytest.raises(OSError):old_probe.bind(('127.0.0.1',port))
    launcher.require_free_ports((port,))
    with socket.socket() as live:
        live.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        live.bind(('127.0.0.1',port));live.listen(1)
        with pytest.raises(ValueError,match='local_port_already_in_use'):
            launcher.require_free_ports((port,))
        # Occupancy checking does not terminate or steal the real listener.
        assert live.getsockname()[1]==port


def test_cleanup_stops_only_owned_process(tmp_path):
    log=(tmp_path/'child.log').open('wb')
    own=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.PIPE,
                         stdout=log,stderr=log,start_new_session=True)
    other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,start_new_session=True)
    row={'name':'masks'}
    try:
        launcher.stop_owned([(row,own,log)])
        assert own.poll() is not None and other.poll() is None and log.closed
        assert row['returncode']==own.returncode
    finally:
        other.terminate();other.wait(timeout=2)


def test_remote_stdin_eof_requests_clean_native_shutdown():
    child_code='import signal,sys,time;signal.signal(signal.SIGINT,lambda *_:sys.exit(0));print("ready",flush=True);time.sleep(60)'
    guard=subprocess.Popen([sys.executable,'-c',launcher.REMOTE_GUARD,sys.executable,'-c',child_code],
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        assert guard.stdout.readline()==b'ready\n'
        guard.stdin.close()
        assert guard.wait(timeout=3)==0
    finally:
        if guard.poll() is None:guard.kill();guard.wait(timeout=2)
        guard.stdout.close();guard.stderr.close()
