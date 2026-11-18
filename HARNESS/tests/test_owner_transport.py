import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from f1_harness.ipc import UnixServer
from f1_harness.owner_transport import OwnerChannel
from f1_harness.owner_transport_server import serve


def command(socket):
    return [sys.executable, '-u', '-c',
        'import sys; from pathlib import Path; from f1_harness.ipc import UnixClient; '
        'from f1_harness.owner_transport_server import serve; '
        'raise SystemExit(serve(UnixClient(Path(sys.argv[1]),timeout=.3),sys.stdin.buffer,sys.stdout,sys.stderr,streaming=True))',
        str(socket)]


def test_same_stream_queries_real_ipc_and_preserves_rpc_errors(tmp_path):
    seen = []
    def dispatch(method, kwargs):
        seen.append((method, kwargs))
        if kwargs.get('reject'):
            raise ValueError('wrong_owner_identity')
        return dict(method=method, binding=kwargs)
    socket = tmp_path/'owner.sock'
    server = UnixServer(socket, dispatch).start()
    channel = OwnerChannel(command(socket), tmp_path/'stderr')
    try:
        assert channel.call('status') == dict(method='status', binding={})
        pid = channel.process.pid
        with pytest.raises(RuntimeError, match='wrong_owner_identity'):
            channel.call('web_start', reject=True)
        for i in range(3):
            assert channel.call('web_control_status', request_id=str(i))['binding']['request_id'] == str(i)
            assert channel.process.pid == pid
    finally:
        channel.close()
        server.close()
    assert len(seen) == 5
    assert channel.process is None
    with pytest.raises(RuntimeError, match='channel_closed'):
        channel.call('status')


def test_lost_mutation_reply_is_not_resent_and_next_status_reconnects(tmp_path):
    script = tmp_path/'fake.py'
    received = tmp_path/'received'
    script.write_text('''import sys,json,time
from pathlib import Path
for line in sys.stdin:
 r=json.loads(line)
 with Path(sys.argv[1]).open('a') as f:f.write(r['method']+'\\n')
 if r['method']=='web_start':time.sleep(10)
 print(json.dumps(dict(id=r['id'],ok=True,result={'known':True})),flush=True)
''')
    channel = OwnerChannel([sys.executable, '-u', str(script), str(received)], tmp_path/'stderr', timeout=.2)
    try:
        with pytest.raises(TimeoutError, match='result_unknown'):
            channel.call('web_start', request_id='original')
        assert channel.process is None
        assert channel.call('web_control_status', request_id='original') == {'known': True}
        assert received.read_text().splitlines() == ['web_start', 'web_control_status']
    finally:
        channel.close()


@pytest.mark.parametrize('reply', ['wrong_id', 'oversize', 'two_frames', 'eof'])
def test_invalid_reply_discards_connection_without_replay(tmp_path, reply):
    script = "import sys,json; r=json.loads(sys.stdin.readline()); "
    script += {'wrong_id':"print(json.dumps(dict(id='0'*32,ok=True,result={})),flush=True)",
              'oversize':"print('x'*70000,flush=True)",
              'two_frames':"print('{}\\n{}',flush=True)", 'eof':"sys.exit(0)"}[reply]
    channel = OwnerChannel([sys.executable, '-u', '-c', script], tmp_path/'stderr', timeout=.5)
    try:
        with pytest.raises((ValueError, ConnectionError)):
            channel.call('web_stop')
        assert channel.process is None
    finally:
        channel.close()


@pytest.mark.parametrize('bad', [
    dict(method='execute_skill',kwargs={},id='1'*32),
    dict(method='web_start',kwargs=[],id='1'*32),
    dict(method='web_start',kwargs={},id='wrong'),
    dict(method='status',kwargs={},id='1'*32,extra=True),
])
def test_server_rejects_malformed_or_nonprivate_method_before_dispatch(bad):
    calls = []
    client = SimpleNamespace(call=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        serve(client, io.BytesIO((json.dumps(bad)+'\n').encode()), io.StringIO(), io.StringIO(), streaming=True)
    assert calls == []


def test_oneshot_remains_compatible_and_eof_does_not_dispatch():
    calls = []
    def call(method, **kwargs):
        calls.append(method)
        return {'ok': 1}
    output = io.StringIO()
    assert serve(SimpleNamespace(call=call),io.BytesIO(b'{"method":"status","kwargs":{}}'),
                 output,io.StringIO(),streaming=False) == 0
    assert json.loads(output.getvalue()) == {'ok': 1}
    assert serve(SimpleNamespace(call=call),io.BytesIO(),io.StringIO(),io.StringIO(),streaming=True) == 0
    assert calls == ['status']


def test_stop_uses_independent_lane_from_status(tmp_path, monkeypatch):
    from f1_harness.web_process import WorkspaceProcesses
    blocked, release = threading.Event(), threading.Event()
    class Channel:
        def __init__(self, argv, log): self.log = log
        def call(self, method, **kwargs):
            if method == 'status':
                blocked.set()
                assert release.wait(2)
            return method
    monkeypatch.setattr('f1_harness.owner_transport.OwnerChannel', Channel)
    processes = WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.config = {'remote_bundle': '/bundle'}
    processes.run, processes.ssh = tmp_path, ['ssh', 'fixture']
    processes.owner_channels, processes.owner_channel_lock = {}, threading.Lock()
    thread = threading.Thread(target=processes.owner_call, args=('status',))
    thread.start()
    try:
        assert blocked.wait(1)
        assert processes.owner_call('web_stop') == 'web_stop'
        assert len(processes.owner_channels) == 2
    finally:
        release.set()
        thread.join(2)
