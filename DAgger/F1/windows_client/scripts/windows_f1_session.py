"""One operator console; SSH owns forwarding and the interactive F1 entry.

The existing sender runs in an owned background process. No Enter, START or
robot action is injected. Neither existing tunnels nor senders are killed.
"""
from __future__ import annotations

import argparse
import ctypes
import codecs
import json
import os
import queue
from pathlib import Path
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import tempfile

ROBOT_ENTRY = '/home/user/Bruno/DAgger/start_f1_toys_inference.sh'
DEFAULT_ROBOT_HOSTS = ('user@192.0.2.7', 'user@192.0.2.3')
LAUNCHER_VERSION = 'development'  # Replaced by the versioned package builder.
DISPLAY = queue.Queue(maxsize=128)
DISPLAY_STARTED = False
CONSOLE_LOCK = threading.Lock()


class ConsoleText:
    """Unicode Windows console output, UTF-8 bytes only for pipes/files.

    Avoid Python buffered stdout locks in daemon threads during shutdown.
    Windows code pages are deliberately left untouched.
    """
    def __init__(self, kernel=None, fd=None):
        self.fd = sys.stdout.fileno() if fd is None else fd
        self.kernel, self.handle = None, None
        if kernel is not None or sys.platform == 'win32':
            from ctypes import wintypes
            if kernel is None:
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.GetStdHandle.argtypes = [wintypes.DWORD]
                kernel.GetStdHandle.restype = wintypes.HANDLE
                kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel.WriteConsoleW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
                kernel.WriteConsoleW.restype = wintypes.BOOL
            handle = kernel.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                self.kernel, self.handle = kernel, handle

    def write(self, text):
        with CONSOLE_LOCK:
            if self.kernel is None:
                data = text.encode('utf-8', 'replace')
                while data:
                    written = os.write(self.fd, data)
                    if not written:raise OSError('console write made no progress')
                    data = data[written:]
                return
            from ctypes import wintypes
            # Length is UTF-16 code units, including surrogate pairs.
            data = text.encode('utf-16-le', 'replace')
            offset = 0
            while offset < len(data):
                part = data[offset:offset+8192]
                if 0xd800 <= int.from_bytes(part[-2:], 'little') <= 0xdbff:
                    part = part[:-2]
                buffer = ctypes.create_string_buffer(part)
                written = wintypes.DWORD()
                if not self.kernel.WriteConsoleW(self.handle, buffer, len(part)//2, ctypes.byref(written), None):
                    raise OSError('WriteConsoleW failed')
                if not written.value:raise OSError('console write made no progress')
                offset += written.value*2


def _display():
    output = ConsoleText()
    while True:
        text = DISPLAY.get()
        try:
            output.write(text.replace('\n','\r\n')+'\r\n')
        except (OSError, ValueError):
            pass
        finally:
            DISPLAY.task_done()


def report(text):
    global DISPLAY_STARTED
    if not DISPLAY_STARTED:
        DISPLAY_STARTED = True
        threading.Thread(target=_display, name='f1-console-notices', daemon=True).start()
    try:
        DISPLAY.put_nowait(text)
    except queue.Full:
        pass  # Complete evidence is in session logs, never block control on UI.


class RawConsole:
    def __init__(self, output=None):
        self.output = output or ConsoleText()
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
    def write(self, data):
        self.output.write(self.decoder.decode(data))
    def flush(self):
        pass


def ssh_command(host, model='rlt400', shadow=False, terminal_size=None):
    # No user-provided shell text; host is a single validated SSH destination.
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', host):
        raise ValueError('Robot host must be user@hostname or user@IPv4.')
    if model not in ('rlt400', 'stageb1250'):
        raise ValueError('Unsupported model')
    command = f'bash {ROBOT_ENTRY} --model {model} --continuous'
    if model == 'stageb1250':
        command += ' --mode hil'
    if shadow:
        command += ' --shadow'
    if terminal_size is not None:
        columns, rows = terminal_size
        if not (isinstance(columns, int) and isinstance(rows, int) and
                20 <= columns <= 1000 and 5 <= rows <= 500):
            raise ValueError('Invalid terminal size')
        command = f'stty cols {columns} rows {rows}; exec ' + command
    return ['ssh.exe', '-F', 'NUL', '-tt',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', host, command]


def tunnel_command(host):
    ssh_command(host)  # Validate destination; no terminal shares this TCP stream.
    return ['ssh.exe','-F','NUL','-N','-T','-o','BatchMode=yes',
            '-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=10',
            '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
            '-L','127.0.0.1:7907:127.0.0.1:7906',host]


def select_robot_host(explicit=None, *, environment=None, connect=None, timeout=1.5):
    """Choose once before starting SSH; never migrate a running session.

    TCP reachability is only a startup hint. OpenSSH still verifies the host
    key and authenticates normally; authentication failures do not fall back.
    """
    environment = os.environ if environment is None else environment
    override = explicit if explicit is not None else environment.get('F1_SSH_HOST')
    if override:
        ssh_command(override)  # Apply the same destination validation as SSH.
        return override, 'argument' if explicit is not None else 'environment'
    if explicit is not None:
        raise ValueError('Robot host must be user@hostname or user@IPv4.')
    connect = socket.create_connection if connect is None else connect
    failures = []
    for host, source in zip(DEFAULT_ROBOT_HOSTS, ('wired', 'wifi-fallback')):
        try:
            with connect((host.split('@', 1)[1], 22), timeout=timeout):
                return host, source
        except OSError as exc:
            failures.append(f'{host}: {exc}')
    raise RuntimeError('F1 有线与 Wi-Fi 备用 SSH 均不可达；检查网络后重新启动。' +
                       ' / '.join(failures))


def check_port(port=7907):
    with socket.socket() as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError as exc:
            raise RuntimeError('7907 已占用：请先退出旧隧道或另一启动器；不会自动关闭它。') from exc


def check_old_sender():
    query = ("$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -match '^python(w)?\\.exe$' -and "
             "$_.CommandLine -match '03_windows_(f1|shadow)_sender\\.py' } | "
             "Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress")
    # No captured pipe: a descendant retaining it can make communicate() hang
    # even after the PowerShell timeout. File handles do not require pipe EOF.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query],
                                   stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stop_owned(process)
            raise RuntimeError('旧sender检查未完成；本次未启动连接。详情已记录并自动同步。')
        if code:
            raise RuntimeError('无法确认旧 sender 是否已退出，请检查 Windows 进程查询权限。')
        stdout.seek(0);value=stdout.read(16384).decode('utf-8-sig','replace').strip()
        if value and json.loads(value):
            raise RuntimeError('旧 sender 仍在运行：请先结束当前任务，再 Ctrl+C 退出旧 sender。')


def sender_worker(parent_pid):
    """Exit our own process if the launcher window is closed, without PID kills."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE only
    if not handle:
        return 2
    def watch():
        result = kernel.WaitForSingleObject(handle, 0xffffffff)
        kernel.CloseHandle(handle)
        # Fail closed on a lost parent or a failed wait; only exit this worker.
        os._exit(0 if result == 0 else 2)
    threading.Thread(target=watch, daemon=True).start()
    sys.argv = [str(Path(__file__).with_name('03_windows_f1_sender.py')),
                '--host', '127.0.0.1', '--port', '7907']
    runpy.run_path(sys.argv[0], run_name='__main__')
    return 0


def stop_owned(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


READY_MARKERS = (
    b'F1 RECEIVER READY v1 ',
    b'F1 POWERED (run): 127.0.0.1:7906; waiting for START;',
    b'F1 SHADOW (policy-shadow): 127.0.0.1:7906; waiting for START;',
    b'F1 SUPERVISED SESSION REATTACH:',
)


class ConsoleModes:
    """Enable ANSI and disable selection-pause only for this console session."""
    def __init__(self, kernel=None):
        self.kernel = kernel
        self.saved = []

    def __enter__(self):
        from ctypes import wintypes
        if self.kernel is None:
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            self.kernel.GetStdHandle.restype = wintypes.HANDLE
            self.kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self.kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        for stream, add, remove in [(-10, 0x80, 0x40), (-11, 0x04, 0)]:
            handle = self.kernel.GetStdHandle(stream)
            mode = wintypes.DWORD()
            if self.kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                if self.kernel.SetConsoleMode(handle, (mode.value | add) & ~remove):
                    self.saved.append((handle, mode.value))
                else:
                    report('[显示] 无法设置终端模式；若标题显示“选择”，请按 Esc 退出选择。')
        return self

    def __exit__(self, *_):
        for handle, mode in reversed(self.saved):
            self.kernel.SetConsoleMode(handle, mode)


def relay_output(pipe, ready, errors, output, *, console_log=None, diagnostics=None):
    """Tee the actual remote console; start no sender before the listen marker."""
    tail = b''
    frames = queue.Queue(maxsize=64)
    finished = threading.Event()
    dropped = [0]
    def display():
        while not finished.is_set() or not frames.empty():
            try:chunk=frames.get(timeout=.1)
            except queue.Empty:continue
            try:
                if dropped[0]:
                    output.write(('\x1b[0m\n[显示] 终端落后，已跳过%d字节重绘；完整原文保留在console.log。\n'%dropped[0]).encode())
                    dropped[0]=0
                output.write(chunk);output.flush()
            except (OSError,ValueError):pass
            finally:frames.task_done()
    painter=threading.Thread(target=display,name='f1-terminal-painter',daemon=True);painter.start()
    try:
        while True:
            chunk = pipe.read1(4096)
            if not chunk:
                return
            if console_log is not None:
                try:
                    console_log.write(chunk); console_log.flush()
                except OSError:
                    pass
            tail = (tail + chunk)[-8192:]
            if any(marker in tail for marker in READY_MARKERS):
                ready.set()
            if diagnostics is not None:
                import re
                matches = re.findall(rb'F1 RECEIVER READY v1 (f1_hil_[A-Za-z0-9_]+)[\r\n]', tail)
                if not matches:
                    matches = re.findall(rb'F1 POWERED[^\r\n]*raw=[^\r\n]*/(f1_hil_[A-Za-z0-9_]+)[\r\n]', tail)
                if matches:
                    diagnostics.accept_ready(matches[-1].decode('ascii'))
            try:frames.put_nowait(chunk)
            except queue.Full:dropped[0]+=len(chunk)
    except Exception as exc:
        errors.append(exc)
    finally:
        finished.set();painter.join(timeout=.3)


def supervise(sender_args, ssh_args, log, env, *, popen=subprocess.Popen,
              sleep=time.sleep, windows=True, output=None, diagnostics=None,
              console_log=None, event=None, clock=time.monotonic, shutdown_timeout=15.,
              tunnel_args=None, ssh_log=None, tunnel_log=None):
    sender = ssh = tunnel = None
    relay = None
    ready, errors = threading.Event(), []
    output = RawConsole() if output is None else output
    event = event or (lambda *a, **k: None)
    stopping = False
    deadline = None
    try:
        if tunnel_args:
            tunnel = popen(tunnel_args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                           stderr=tunnel_log if tunnel_log is not None else subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW if windows else 0)
            event('transport_ssh_started',pid=tunnel.pid)
        # Password prompts remain on SSH's inherited stderr/console. Its stdin
        # remains the real terminal, including Enter and Ctrl+C; stdout is teed.
        ssh = popen(ssh_args, stdout=subprocess.PIPE, stderr=ssh_log)
        event('ssh_started', pid=ssh.pid)
        relay = threading.Thread(target=relay_output,
                                 args=(ssh.stdout, ready, errors, output),
                                 kwargs=dict(console_log=console_log, diagnostics=diagnostics), daemon=True)
        relay.start()
        while True:
            try:
                code = ssh.poll()
                if code is not None:
                    event('ssh_exited', code=code)
                    return code
                if tunnel is not None and tunnel.poll() is not None:
                    event('transport_ssh_exited',code=tunnel.poll())
                    report('手柄隧道已退出；停止本次sender。连接原因保留在transport-ssh.log。')
                    return tunnel.poll() or 1
                if diagnostics is not None and diagnostics.receiver_ended.is_set():
                    event('receiver_exited', run_id=diagnostics.expected_run)
                    report('\nF1 接收端已退出；正在释放本次手柄和隧道。')
                    return 0
                remote_stopping = getattr(diagnostics, 'receiver_stopping', None)
                if not stopping and remote_stopping is not None and remote_stopping.is_set():
                    stopping = True
                    deadline = clock()+shutdown_timeout
                    event('receiver_stopping', run_id=diagnostics.expected_run)
                    stop_owned(sender); sender = None
                    report('\nF1 已进入退出／保存流程；手柄停止重连，等待保存后返回 PowerShell。')
                if stopping and clock() >= deadline:
                    event('local_shutdown_timeout', remote_exit_confirmed=False)
                    report('\n本地会话已结束；远端保存未确认，将保留日志。再次运行同一命令可重新连接。')
                    return 130
                if errors:
                    # Receive-side failure is handled by SSH's own exit and
                    # the robot input watchdog; never terminate on display I/O.
                    errors.clear()
                if not stopping and sender is None and ready.is_set():
                    sender = popen(sender_args, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, env=env,
                                   creationflags=subprocess.CREATE_NO_WINDOW if windows else 0)
                    report('F1 接收端已监听，正在启动本地手柄；等模式确认后按 START。')
                    event('sender_started', pid=sender.pid)
                if sender is not None and sender.poll() is not None:
                    # A new sender has a new handshake/session. The robot
                    # remains paused until genuine neutral/START/RB inputs.
                    event('sender_exited', code=sender.poll(), stopping=stopping)
                    sender = None
                    if not stopping:
                        sleep(2.)
                sleep(.2)
            except KeyboardInterrupt:
                if stopping:
                    event('second_interrupt', remote_exit_confirmed=False)
                    report('\n结束本地连接；远端仍按自身停止／保存流程处理。')
                    return 130
                stopping = True
                deadline = clock()+shutdown_timeout
                event('operator_ctrl_c')
                if diagnostics is not None:
                    diagnostics.stop_requested.set()
                stop_owned(sender)
                sender = None
                report('\n正在退出：本地手柄已停止重连，等待 F1 保存；随后返回当前 PowerShell。')
    finally:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            stop_owned(sender)
            stop_owned(ssh)
            stop_owned(tunnel)
            if relay is not None:
                relay.join(timeout=2)
            event('local_children_reaped')
        finally:
            signal.signal(signal.SIGINT, previous)


def key_path():
    return Path.home()/'.ssh/f1_operator_ed25519'


def setup_key(host):
    """Run once on the operator PC; only its public key leaves Windows."""
    import base64
    import re
    ssh_command(host)  # Same destination validation as normal launch.
    key = key_path()
    key.parent.mkdir(exist_ok=True)
    if not key.exists():
        subprocess.run(['ssh-keygen.exe', '-t', 'ed25519', '-N', '', '-f', str(key),
                        '-C', 'f1-windows-operator'], check=True)
    public = key.with_suffix('.pub')
    if not public.exists():
        raise RuntimeError('专用密钥已有但公钥缺失；不会覆盖，请检查 '+str(key))
    line = public.read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', line):
        raise RuntimeError('专用公钥格式不正确。')
    # The installation command contains a public key only, never a password.
    payload = base64.b64encode(line.encode()).decode('ascii')
    code = ("from pathlib import Path;import base64,os;"
            "d=Path.home()/'.ssh';d.mkdir(mode=0o700,exist_ok=True);"
            "p=d/'authorized_keys';p.touch(mode=0o600,exist_ok=True);"
            "k=base64.b64decode('"+payload+"').decode();"
            "old=p.read_text();parts=k.split();"
            "present=any(parts[1] in row.split() for row in old.splitlines() if not row.lstrip().startswith('#'));"
            "f=p.open('a');f.write(('' if not old or old.endswith('\\n') else '\\n')+"
            "'no-agent-forwarding,no-X11-forwarding '+k+'\\n') if not present else None;f.close();"
            "d.chmod(0o700);p.chmod(0o600);print('F1 public key installed')")
    import shlex
    print('只需这次按 SSH 提示输入机器人密码；私钥留在本机，不记录密码。', flush=True)
    subprocess.run(['ssh.exe', '-F', 'NUL', host, 'python3 -c '+shlex.quote(code)], check=True)
    subprocess.run(['ssh.exe', '-F', 'NUL', '-i', str(key), '-o', 'IdentitiesOnly=yes',
                    '-o', 'BatchMode=yes', host, 'true'], check=True)
    print('免密码验证通过。以后直接双击 start_f1_rlt.cmd。', flush=True)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--robot-host', help='固定 SSH 目标；否则采用 F1_SSH_HOST，未设置时启动前依次探测有线／Wi-Fi')
    p.add_argument('--model', choices=['rlt400', 'stageb1250'], default='rlt400')
    p.add_argument('--shadow', action='store_true')
    p.add_argument('--setup-key', action='store_true', help='一次性设置 Windows→F1 SSH 密钥登录')
    p.add_argument('--sender-worker', type=int, help=argparse.SUPPRESS)
    args = p.parse_args()
    if sys.platform != 'win32':
        p.error('This operator launcher runs on Windows only.')
    if args.sender_worker is not None:
        return sender_worker(args.sender_worker)
    args.robot_host, host_selection = select_robot_host(args.robot_host)
    selection_labels = {'argument': '命令行指定', 'environment': 'F1_SSH_HOST 指定',
                        'wired': '有线优先', 'wifi-fallback': '有线不可达，Wi-Fi 备用'}
    report('连接目标    ' + args.robot_host + '（' + selection_labels[host_selection] + '；本次会话固定）')
    if args.setup_key:
        return setup_key(args.robot_host)
    command = ssh_command(args.robot_host, args.model, args.shadow,
                          terminal_size=tuple(shutil.get_terminal_size((120, 40))))
    if key_path().exists():
        command[1:1] = ['-i', str(key_path()), '-o', 'IdentitiesOnly=yes']
    if not shutil.which('ssh.exe'):
        raise RuntimeError('未找到 Windows OpenSSH ssh.exe。')
    root = Path(__file__).resolve().parents[1]
    if not (root/'scripts/03_windows_f1_sender.py').is_file():
        raise RuntimeError('请安装在现有 DAgger-F1 客户端内。')
    # Windows releases this file lock even if the console is closed unexpectedly.
    import msvcrt
    runtime = Path(os.environ.get('DAGGER_CLIENT_ROOT', str(root)))/'runtime'
    runtime.mkdir(exist_ok=True)
    with (runtime/'single-window.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0');lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('另一个单窗口启动器仍在运行。') from exc
        # Reuse the very same interpreter and import root as windows_f1_sender.cmd.
        env = dict(os.environ, PYTHONPATH=str(root/'src'), PYTHONUNBUFFERED='1',
                   PYTHONIOENCODING='utf-8')
        session = runtime/'operator-sessions'/uuid.uuid4().hex
        session.mkdir(parents=True)
        from windows_session_logs import Diagnostics, log_event
        event = lambda name, **details: log_event(session, name, **details)
        launcher_label = 'F1 SSH＋手柄会话 ' + LAUNCHER_VERSION
        event('launcher_started', version=launcher_label, model=args.model,
              host=args.robot_host, host_selection=host_selection, shadow=args.shadow, pid=os.getpid())
        package_identity = root/'operator-package.json'
        if package_identity.exists():
            identity = json.loads(package_identity.read_text())
            event('operator_package_loaded', **identity)
            report('操控包／sender  '+identity['version']+' · '+identity['operator_protocol'])
        log_path = session/'sender.log'
        tunnel = tunnel_command(args.robot_host)
        if key_path().exists():
            tunnel[1:1] = ['-i',str(key_path()),'-o','IdentitiesOnly=yes']
        diagnostic_ssh = ['ssh.exe', '-F', 'NUL', '-T', '-o', 'BatchMode=yes',
                          '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=3',
                          '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2']
        if key_path().exists():
            diagnostic_ssh += ['-i', str(key_path()), '-o', 'IdentitiesOnly=yes']
        diagnostics = Diagnostics(session, diagnostic_ssh+[args.robot_host], args.model, report)
        report('启动器版本  ' + launcher_label + '（本机；与机器人控制版本分别编号）')
        report('控制版本    等待机器人运行端返回实际身份；不能由启动器版本推断。')
        report('F1 单窗口启动：独立手柄隧道；终端显示与日志不共用控制SSH。')
        report(('RLT 无需 Enter；看到“手柄已连接，等待 START”后按 START。' if args.model == 'rlt400' else
                '现场 Enter 后再 START。') + '结束先 Y 保存，再 Ctrl+C 等退出。')
        report('连接日志：'+str(session)+'（后台同步 F1／6000D，断线保留补传）')
        report('请先插好 F710 接收器并切到 X 模式；无手柄时会停在等待 START。')
        diagnostics.start()
        try:
            event('sender_check_started')
            check_old_sender()
            event('sender_check_completed')
            check_port()
            event('local_port_available')
            with ConsoleModes(), log_path.open('ab', buffering=0) as log, (session/'console.log').open('ab', buffering=0) as console, (session/'ssh.log').open('ab', buffering=0) as ssh_log, (session/'transport-ssh.log').open('ab', buffering=0) as tunnel_log:
                event('console_ready')
                return supervise([sys.executable, '-u', str(Path(__file__).resolve()),
                                  '--sender-worker', str(os.getpid())], command, log, env,
                                  diagnostics=diagnostics, console_log=console, event=event,tunnel_args=tunnel,
                                  ssh_log=ssh_log,tunnel_log=tunnel_log)
        except Exception as exc:
            event('launcher_error', kind=type(exc).__name__, reason=str(exc))
            raise
        finally:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                event('launcher_finished')
                diagnostics.close()
            finally:
                signal.signal(signal.SIGINT, previous)
            report('本次本地进程已清理；可在这个 PowerShell 再次运行 start_f1_rlt.cmd。')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print('启动失败：'+str(exc), file=sys.stderr)
        raise SystemExit(1)
