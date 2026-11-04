import importlib.util
from pathlib import Path
import socket
import subprocess

import pytest

PATH = Path(__file__).resolve().parents[1]/'windows_client/scripts/windows_f1_session.py'
spec = importlib.util.spec_from_file_location('single_window', PATH)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_display_and_transport_use_distinct_ssh_connections():
    args = launcher.ssh_command('user@192.0.2.3')
    assert '-tt' in args and '-N' not in args
    assert '-L' not in args
    tunnel=launcher.tunnel_command('user@192.0.2.3')
    assert '-N' in tunnel and '-T' in tunnel and 'BatchMode=yes' in tunnel
    assert tunnel[tunnel.index('-L')+1] == '127.0.0.1:7907:127.0.0.1:7906'
    assert args[-1] == 'bash /home/user/Bruno/DAgger/start_f1_toys_inference.sh --model rlt400 --continuous'
    assert 'StrictHostKeyChecking=no' not in args
    assert launcher.ssh_command('user@host', 'stageb1250', True)[-1].endswith('--mode hil --shadow')
    with pytest.raises(ValueError):
        launcher.ssh_command('user@host; echo injected')


def test_busy_port_does_not_reuse_or_stop_listener():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0));listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match='占用'):
            launcher.check_port(port)
        with socket.create_connection(('127.0.0.1', port)):
            connection, _ = listener.accept();connection.close()


@pytest.mark.parametrize('wired_available, expected_source', [(True, 'wired'), (False, 'wifi-fallback')])
def test_startup_host_selection_prefers_wired_and_closes_probe(wired_available, expected_source):
    calls, closed = [], []
    class Probe:
        def __enter__(self):return self
        def __exit__(self, *_):closed.append(True)
    def connect(address, *, timeout):
        calls.append((address, timeout))
        if address[0] == '192.0.2.7' and not wired_available:
            raise TimeoutError('unreachable')
        return Probe()
    host, source = launcher.select_robot_host(environment={}, connect=connect)
    assert (host, source) == (launcher.DEFAULT_ROBOT_HOSTS[not wired_available], expected_source)
    assert [address for address, _ in calls] == [('192.0.2.7', 22)] + ([] if wired_available else [('192.0.2.3', 22)])
    assert all(0 < timeout <= 2 for _, timeout in calls)
    assert closed == [True]
    # All session channels receive the single chosen target.
    assert launcher.ssh_command(host)[-2] == launcher.tunnel_command(host)[-1] == host


@pytest.mark.parametrize('explicit, environment, expected', [
    ('user@192.0.2.3', {'F1_SSH_HOST': 'user@192.0.2.7'}, ('user@192.0.2.3', 'argument')),
    (None, {'F1_SSH_HOST': 'user@site-alias'}, ('user@site-alias', 'environment')),
])
def test_explicit_host_is_preserved_without_probing_or_fallback(explicit, environment, expected):
    def unexpected_probe(*args, **kwargs):raise AssertionError('explicit destination must remain fixed')
    assert launcher.select_robot_host(explicit, environment=environment, connect=unexpected_probe) == expected


def test_unreachable_default_hosts_fail_before_starting_a_session():
    calls = []
    def connect(address, **kwargs):
        calls.append(address)
        raise ConnectionRefusedError('refused')
    with pytest.raises(RuntimeError, match='均不可达'):
        launcher.select_robot_host(environment={}, connect=connect)
    assert calls == [('192.0.2.7', 22), ('192.0.2.3', 22)]


@pytest.mark.parametrize('host', ['', 'user@host; echo injected'])
def test_invalid_explicit_host_is_rejected_before_probing(host):
    def unexpected_probe(*args, **kwargs):raise AssertionError('invalid destination must not be probed')
    with pytest.raises(ValueError):
        launcher.select_robot_host(host, environment={}, connect=unexpected_probe)


def run_session(tmp_path, remote_code, sender_code):
    import io, os, sys
    children = []
    def spawn(*a, **kw):
        child = subprocess.Popen(*a, **kw);children.append(child);return child
    with (tmp_path/'log').open('wb') as log:
        try:
            code = launcher.supervise([sys.executable, '-u', '-c', sender_code],
                [sys.executable, '-u', '-c', remote_code], log, dict(os.environ),
                popen=spawn, windows=False, output=io.BytesIO())
            return code, children
        finally:
            assert all(child.poll() is not None for child in children)


def test_ssh_failure_before_listener_never_starts_sender(tmp_path):
    code, children = run_session(tmp_path, 'raise SystemExit(7)', 'raise AssertionError("must not start")')
    assert code == 7 and len(children) == 1


def test_sender_starts_only_after_listener_marker(tmp_path):
    flag = tmp_path/'sender-started'
    marker = launcher.READY_MARKERS[0].decode()
    remote = ("from pathlib import Path;import time;"
              f"flag=Path({str(flag)!r});print('Enter authorization first',flush=True);"
              "time.sleep(.25);assert not flag.exists();"
              f"print({marker!r},flush=True);"
              "deadline=time.time()+3\n"
              "while not flag.exists() and time.time()<deadline: time.sleep(.02)\n"
              "assert flag.exists()")
    sender = f"from pathlib import Path;import time;Path({str(flag)!r}).touch();time.sleep(30)"
    code, children = run_session(tmp_path, remote, sender)
    assert code == 0 and len(children) == 2


def test_sender_failure_restarts_sender_without_terminating_remote(tmp_path):
    marker = launcher.READY_MARKERS[0].decode()
    code, children = run_session(tmp_path,
        f"import time;print({marker!r},flush=True);time.sleep(3)", 'raise SystemExit(2)')
    assert code == 0 and len(children) >= 3


def test_split_readiness_marker_and_console_are_preserved():
    import io, threading
    class Pipe:
        def __init__(self): self.parts=iter([b'prompt\n',launcher.READY_MARKERS[0][:20],launcher.READY_MARKERS[0][20:],b''])
        def read1(self, _): return next(self.parts)
    ready, output, errors = threading.Event(), io.BytesIO(), []
    launcher.relay_output(Pipe(), ready, errors, output)
    assert ready.is_set() and not errors
    assert output.getvalue()==b'prompt\n'+launcher.READY_MARKERS[0]


def test_blocked_display_cannot_block_ready_or_raw_log():
    import io, threading
    entered, release, ready = threading.Event(), threading.Event(), threading.Event()
    class Display:
        def write(self, data):entered.set();release.wait(3)
        def flush(self):pass
    raw = b'x'*400000+launcher.READY_MARKERS[0]
    source=io.BytesIO(raw);log=io.BytesIO();errors=[]
    try:
        worker=threading.Thread(target=launcher.relay_output,args=(source,ready,errors,Display()),kwargs={'console_log':log})
        worker.start();worker.join(1)
        assert entered.is_set() and not worker.is_alive()
        assert ready.is_set() and log.getvalue()==raw and not errors
    finally:release.set();worker.join(4)


def test_failed_transport_closes_owned_terminal_without_starting_sender(tmp_path):
    import io, os, sys
    children=[];events=[]
    def spawn(*a,**kw):
        p=subprocess.Popen(*a,**kw);children.append(p);return p
    with (tmp_path/'log').open('wb') as log:
        code=launcher.supervise([sys.executable,'-c','raise AssertionError("sender")'],
            [sys.executable,'-c','import time;time.sleep(30)'],log,dict(os.environ),
            popen=spawn,windows=False,output=io.BytesIO(),event=lambda name,**kw:events.append(name),
            tunnel_args=[sys.executable,'-c','raise SystemExit(9)'])
    assert code==9 and len(children)==2 and all(p.poll() is not None for p in children)
    assert 'transport_ssh_exited' in events and 'sender_started' not in events


def test_key_setup_appends_public_key_idempotently_without_password(tmp_path, monkeypatch):
    import shlex
    key=tmp_path/'key';key.write_text('existing private key stays untouched')
    key.with_suffix('.pub').write_text('ssh-ed25519 AAAATEST f1-unit-test\n')
    home=tmp_path/'remote';(home/'.ssh').mkdir(parents=True)
    authorized=home/'.ssh/authorized_keys';authorized.write_text('ssh-ed25519 AAAAOLD other-key')
    calls=[]
    monkeypatch.setattr(launcher,'key_path',lambda:key)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    def run(args, **kwargs):
        calls.append(args)
        if args[-1].startswith('python3 -c '): exec(shlex.split(args[-1])[2], {})
        return subprocess.CompletedProcess(args,0)
    monkeypatch.setattr(launcher.subprocess,'run',run)
    launcher.setup_key('user@host');launcher.setup_key('user@host')
    assert authorized.read_text().splitlines()==['ssh-ed25519 AAAAOLD other-key',
        'no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAATEST f1-unit-test']
    assert key.read_text()=='existing private key stays untouched'
    assert all('BatchMode=yes' in c for c in calls if c[-1]=='true')
    assert not any('existing private key' in str(c) for c in calls)


def test_ctrl_c_stops_reconnecting_sender_and_waits_for_remote(tmp_path):
    import io, os, sys, time
    children=[];interrupted=False
    marker=launcher.READY_MARKERS[0].decode()
    def spawn(*a, **kw):
        child=subprocess.Popen(*a, **kw);children.append(child);return child
    def sleep(delay):
        nonlocal interrupted
        if len(children)==2 and not interrupted:
            interrupted=True
            raise KeyboardInterrupt
        if interrupted and children[0].poll() is None:
            assert children[1].poll() is not None
        time.sleep(delay)
    with (tmp_path/'log').open('wb') as log:
        code=launcher.supervise([sys.executable,'-c','import time;time.sleep(30)'],
            [sys.executable,'-u','-c',f'import time;print({marker!r},flush=True);time.sleep(.7)'],
            log,dict(os.environ),popen=spawn,sleep=sleep,windows=False,output=io.BytesIO())
    assert code==0 and interrupted and all(c.poll() is not None for c in children)


def test_ctrl_c_is_bounded_when_ssh_never_exits(tmp_path):
    import io, os, sys, time
    children=[];interrupted=False;events=[]
    def spawn(*a, **kw):
        child=subprocess.Popen(*a, **kw);children.append(child);return child
    def sleep(delay):
        nonlocal interrupted
        if len(children)==2 and not interrupted:
            interrupted=True;raise KeyboardInterrupt
        time.sleep(.02)
    marker=launcher.READY_MARKERS[0].decode()
    with (tmp_path/'log').open('wb') as log:
        code=launcher.supervise([sys.executable,'-c','import time;time.sleep(60)'],
            [sys.executable,'-u','-c',f'import time;print({marker!r},flush=True);time.sleep(60)'],
            log,dict(os.environ),popen=spawn,sleep=sleep,windows=False,output=io.BytesIO(),
            shutdown_timeout=.1,event=lambda name,**kw:events.append(name))
    assert code==130 and len(children)==2
    assert all(c.poll() is not None for c in children)
    assert 'local_shutdown_timeout' in events and 'local_children_reaped' in events


@pytest.mark.parametrize('state',['ended','stopping'])
def test_receiver_exit_ends_stale_ssh_without_new_sender(tmp_path,state):
    import io, os, sys, threading, time
    from types import SimpleNamespace
    diag=SimpleNamespace(receiver_ended=threading.Event(),receiver_stopping=threading.Event(), expected_run='f1_hil_test',accept_ready=lambda _:None)
    children=[]
    def spawn(*a,**kw):
        child=subprocess.Popen(*a,**kw);children.append(child);return child
    def sleep(delay):
        if len(children)==2:getattr(diag,'receiver_'+state).set()
        time.sleep(.02)
    with (tmp_path/'log').open('wb') as log:
        code=launcher.supervise([sys.executable,'-c','import time;time.sleep(60)'],
            [sys.executable,'-u','-c',f'import time;print({launcher.READY_MARKERS[0].decode()!r},flush=True);time.sleep(60)'],
            log,dict(os.environ),popen=spawn,sleep=sleep,windows=False,output=io.BytesIO(),diagnostics=diag,shutdown_timeout=.1)
    assert code==(0 if state=='ended' else 130) and len(children)==2 and all(c.poll() is not None for c in children)


def test_repeated_ctrl_c_still_reaps_owned_children(tmp_path):
    import io, os, sys, time
    children=[];interrupts=0
    def spawn(*a,**kw):
        child=subprocess.Popen(*a,**kw);children.append(child);return child
    def sleep(delay):
        nonlocal interrupts
        if len(children)==2 and interrupts<2:
            interrupts+=1;raise KeyboardInterrupt
        time.sleep(.02)
    with (tmp_path/'log').open('wb') as log:
        code=launcher.supervise([sys.executable,'-c','import time;time.sleep(60)'],
            [sys.executable,'-u','-c',f'import time;print({launcher.READY_MARKERS[0].decode()!r},flush=True);time.sleep(60)'],
            log,dict(os.environ),popen=spawn,sleep=sleep,windows=False,output=io.BytesIO())
    assert code==130 and interrupts==2 and all(c.poll() is not None for c in children)


def test_broken_display_keeps_draining_and_detects_reattach():
    import threading
    class Pipe:
        def __init__(self): self.parts=iter([b'hello',launcher.READY_MARKERS[-1],b''])
        def read1(self, _): return next(self.parts)
    class BrokenDisplay:
        def write(self, _): raise BrokenPipeError('closed display')
    ready, errors = threading.Event(), []
    launcher.relay_output(Pipe(), ready, errors, BrokenDisplay())
    assert ready.is_set() and not errors


def test_console_modes_preserve_input_and_restore():
    import ctypes
    from ctypes import wintypes
    class Kernel:
        values={-10: 0x47, -11: 0x03}
        def GetStdHandle(self, stream): return stream
        def GetConsoleMode(self, handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0]=self.values[handle]
            return 1
        def SetConsoleMode(self, handle, value):
            self.values[handle]=value
            return 1
    kernel=Kernel()
    with launcher.ConsoleModes(kernel):
        assert kernel.values[-10] == 0x87  # processed/line/echo unchanged
        assert kernel.values[-11] == 0x07
    assert kernel.values == {-10:0x47,-11:0x03}


def test_actual_console_size_is_numeric_and_no_enter_injected():
    command=launcher.ssh_command('user@host', terminal_size=(144,48))[-1]
    assert command.startswith('stty cols 144 rows 48; exec bash ')
    assert 'send-keys' not in command
    with pytest.raises(ValueError):
        launcher.ssh_command('user@host',terminal_size=('80;bad',24))


def test_unicode_console_uses_utf16_without_changing_codepage():
    import ctypes
    from ctypes import wintypes
    class Kernel:
        def __init__(self):self.output=[]
        def GetStdHandle(self, stream):return 123
        def GetConsoleMode(self, handle, mode):return 1
        def WriteConsoleW(self,handle,data,count,written,reserved):
            self.output.append(ctypes.string_at(data,count*2))
            ctypes.cast(written,ctypes.POINTER(wintypes.DWORD))[0]=count
            return 1
    kernel=Kernel();output=launcher.ConsoleText(kernel=kernel,fd=1)
    text='开始 → 人工接管 ↑ 成功\r\n'+'a'*4095+'🟢'+'继续'
    output.write(text)
    assert b''.join(kernel.output).decode('utf-16-le')==text
    assert all(part.decode('utf-16-le') for part in kernel.output)


def test_remote_utf8_characters_split_across_reads_are_not_corrupted():
    import io
    output=io.StringIO();console=launcher.RawConsole(output)
    raw='\x1b[32m策略执行 → RLT阶段\x1b[0m\r\n'.encode()
    for byte in raw:console.write(bytes([byte]))
    assert output.getvalue()==raw.decode()


def test_redirected_console_preserves_utf8_and_handles_short_writes(monkeypatch):
    class Kernel:
        def GetStdHandle(self, _):return 1
        def GetConsoleMode(self,*_):return 0
    data=[]
    def write(fd,value):data.append(value[:2]);return len(value[:2])
    monkeypatch.setattr(launcher.os,'write',write)
    launcher.ConsoleText(kernel=Kernel(),fd=88).write('提示音\n')
    assert b''.join(data)=='提示音\n'.encode()


def test_packaged_launcher_and_installer_identify_the_same_explicit_version(tmp_path):
    import hashlib, json, runpy, sys, zipfile
    build = runpy.run_path(str(PATH.parents[2]/'scripts/74_package_windows_single_window.py'))['build']
    for version in ('V901', 'V902'):
        output = tmp_path/version
        files = build(output, version)
        client = tmp_path/(version+'-client')
        with zipfile.ZipFile(output/f'windows-single-window-{version}.zip') as archive:
            archive.extractall(client)
        for name, digest in files.items():
            assert hashlib.sha256((client/name).read_bytes()).hexdigest() == digest
        module = runpy.run_path(str(client/'scripts/windows_f1_session.py'))
        assert module['LAUNCHER_VERSION'] == version
        script = (output/'install_single_window.ps1').read_text(encoding='utf-8-sig')
        probe = script.split("$probe=@'\n")[1].split("\n'@")[0]
        checked = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(client)],
                                 capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
        assert json.loads((output/'package.json').read_text())['version'] == version
        assert version in script and '__LAUNCHER_VERSION__' not in script
        wrong = (client/'scripts/windows_f1_session.py').read_text().replace(
            'LAUNCHER_VERSION = '+repr(version), "LAUNCHER_VERSION = 'wrong'")
        (client/'scripts/windows_f1_session.py').write_text(wrong)
        checked = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(client)],
                                 capture_output=True, text=True)
        assert checked.returncode != 0
    with pytest.raises(TypeError):
        build(tmp_path/'no-version')
