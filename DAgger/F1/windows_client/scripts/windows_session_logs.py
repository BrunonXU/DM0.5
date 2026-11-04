"""Durable operator diagnostics over a separate, noninteractive SSH connection.

No passwords, private keys, stdin, environment dump, or robot commands are logged.
The only control operation is a genuine local Ctrl+C, bound to the observed F1
run/PID/start time. Network/log failure cannot block the sender or its cleanup.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time

FILES = ('launcher.jsonl', 'sender.log', 'ssh.log', 'transport-ssh.log', 'console.log')
REMOTE_ROOT = '/home/user/Bruno/DAgger-runtime/operator-logs'
STATUS_PATHS = {
    'rlt400': '/home/user/Bruno/DAgger-runtime/rlt-local-20261027-r2/runtime/status.json',
    'stageb1250': '/home/user/Bruno/DAgger-runtime/stage-ab-20261023/B-1250/hil/runtime/status.json',
}


def receive(request, *, root=REMOTE_ROOT, status_paths=STATUS_PATHS):
    """Called on F1. Restricted append-only files; idempotent acknowledged chunks."""
    import fcntl
    session = request['session']
    if not re.fullmatch(r'[a-f0-9]{32}', session):
        raise ValueError('Invalid diagnostic session')
    target = Path(root)/session
    if Path(root).is_symlink() or target.is_symlink():
        raise ValueError('Diagnostic symlink')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    offsets = {}
    for item in request.get('files', []):
        name, offset = item['name'], item['offset']
        if name not in FILES or type(offset) is not int or not 0 <= offset <= 128*1024*1024:
            raise ValueError('Invalid log range')
        data = base64.b64decode(item['data'], validate=True)
        if len(data) > 65536 or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Invalid log checksum')
        path = target/name
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            size = os.fstat(stream.fileno()).st_size
            if offset > size:
                offsets[name] = size
                continue
            stream.seek(offset)
            existing = stream.read(len(data))
            if data[:len(existing)] != existing:
                raise ValueError('Existing log bytes differ; refusing rewrite')
            if len(existing) < len(data):
                stream.seek(0, 2)
                stream.write(data[len(existing):]); stream.flush(); os.fsync(stream.fileno())
            offsets[name] = offset + len(data)
    result = dict(offsets=offsets, checked_unix=time.time(), receiver=None)
    try:
        status = json.loads(Path(status_paths[request['model']]).read_text())
        pid = int(status['pid'])
        identity = {k: status[k] for k in ('run_id', 'pid', 'start_ticks')}
        alive = False
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            command = Path(f'/proc/{pid}/cmdline').read_bytes()
            alive = (fields[0] != 'Z' and fields[19] == str(status['start_ticks'])
                     and Path(f'/proc/{pid}').stat().st_uid == os.getuid()
                     and b'f1dagger' in command)
        except OSError:
            pass
        fresh = 0 <= time.time()-status['updated_unix'] < 5
        result['receiver'] = dict(identity, alive=alive, fresh=fresh,
                                  state=status['status']['state'])
        # Never infer stop from a dropped network connection. This flag is set
        # only by an actual Ctrl+C in this launcher's console.
        stop = request.get('stop')
        if (stop and stop == identity and alive and fresh and result['receiver']['state'] != 'stopping'
                and 'finish_session_v1' in status.get('capabilities', [])):
            # Reuse the receiver's existing session-bound graceful finish
            # mailbox. No new signal handling, PID kill or robot reset path.
            with (target/'stop-request.json').open('a+') as marker:
                fcntl.flock(marker, fcntl.LOCK_EX); marker.seek(0)
                previous = marker.read()
                if previous:
                    if json.loads(previous) == identity:
                        result['stop_sent'] = identity
                else:
                    finish = Path(status_paths[request['model']]).parent/'finish-request.json'
                    temporary = finish.with_name('finish-request-'+session+'.partial')
                    with temporary.open('w') as stream:
                        json.dump(dict(schema='f1_finish_request_v1', **identity, requested_unix=time.time()),stream)
                        stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary,finish)
                    marker.write(json.dumps(identity)); marker.flush(); os.fsync(marker.fileno())
                    result['stop_sent'] = identity
    except (OSError, ValueError, KeyError, TypeError):
        pass
    receipt = target/'sync.json'
    temporary = target/'sync.partial'
    temporary.write_text(json.dumps(dict(session=session, **result))+'\n')
    os.replace(temporary, receipt)
    return result


def remote_program():
    # Send only this audited receiver implementation, never local credentials.
    import inspect
    return ('import base64,hashlib,json,os,re,time\nfrom pathlib import Path\n'
            + 'FILES='+repr(FILES)+'\nREMOTE_ROOT='+repr(REMOTE_ROOT)+'\n'
            + 'STATUS_PATHS='+repr(STATUS_PATHS)+'\n'+inspect.getsource(receive)
            + '\nprint(json.dumps(receive(json.loads(input()))))\n')


def log_event(directory, event, **details):
    try:
        with (Path(directory)/'launcher.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(time_utc=time.time(), event=event, **details), ensure_ascii=False)+'\n')
    except OSError:
        pass


class Diagnostics:
    def __init__(self, directory, ssh, model, report=print, run=subprocess.run):
        self.directory, self.ssh, self.model = Path(directory), list(ssh), model
        self.report, self.run = report, run
        self.expected_run = None
        self.receiver_identity = None
        self.receiver_ended = threading.Event()
        self.receiver_stopping = threading.Event()
        self.stop_requested = threading.Event()
        self.closed = threading.Event()
        self.stop_sent = False
        self.thread = None
        self.command = self.ssh + ['python3 -c '+shlex.quote(remote_program())]
        self.last_warning = 0.
        self.ssh_offset = 0
        self.deferred_files = {}

    def accept_ready(self, run_id):
        if re.fullmatch(r'f1_hil_[A-Za-z0-9_]+', run_id):
            self.expected_run = run_id

    def sync_once(self, directory=None):
        directory = Path(directory or self.directory)
        ack = directory/'sync-offsets.json'
        offsets = json.loads(ack.read_text()) if ack.exists() else {}
        files = []
        for name in FILES:
            path = directory/name
            try:
                if not path.exists():
                    continue
                offset = offsets.get(name, 0)
                with path.open('rb') as stream:
                    stream.seek(offset); data = stream.read(65536)
            except OSError as exc:
                # One Windows sharing violation must not prevent live status
                # polling or syncing every other diagnostic file.
                key = (str(directory), name)
                reason = type(exc).__name__+': '+str(exc)
                if self.deferred_files.get(key) != reason:
                    log_event(self.directory,'sync_file_deferred',file=name,session=directory.name,reason=reason)
                    self.deferred_files[key] = reason
                continue
            self.deferred_files.pop((str(directory), name), None)
            if data:
                files.append(dict(name=name, offset=offset, data=base64.b64encode(data).decode(),
                                  sha256=hashlib.sha256(data).hexdigest()))
        request = dict(session=directory.name, model=self.model, files=files)
        if directory == self.directory and self.stop_requested.is_set() and not self.stop_sent:
            request['stop'] = self.receiver_identity
        result = self.run(self.command, input=json.dumps(request)+'\n', capture_output=True,
                          text=True, timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('diagnostic SSH exit='+str(result.returncode)+': '+result.stderr[-800:])
        response = json.loads(result.stdout)
        offsets.update(response['offsets'])
        temporary = ack.with_suffix('.partial'); temporary.write_text(json.dumps(offsets)); os.replace(temporary, ack)
        if directory == self.directory:
            receiver = response.get('receiver')
            if receiver and receiver['run_id'] == self.expected_run:
                if receiver['alive'] and receiver['fresh']:
                    self.receiver_identity = {k: receiver[k] for k in ('run_id','pid','start_ticks')}
                if not receiver['alive']:
                    self.receiver_ended.set()
                if receiver['state'] in ('stopping', 'failed'):
                    self.receiver_stopping.set()
            if response.get('stop_sent'):
                self.stop_sent = True
                log_event(directory, 'remote_stop_sent', identity=response['stop_sent'])
        return response

    def start(self):
        def work():
            pending = [p for p in sorted(self.directory.parent.iterdir())
                       if p.is_dir() and re.fullmatch('[a-f0-9]{32}', p.name) and p != self.directory]
            while not self.closed.is_set():
                # ssh -E preserves the raw diagnostics without flooding the
                # terminal. Surface each new batch, retaining every raw byte.
                ssh_log = self.directory/'ssh.log'
                try:
                    with ssh_log.open('rb') as stream:
                        stream.seek(self.ssh_offset); lines = stream.read(65536)
                        self.ssh_offset += len(lines)
                    if lines:
                        decoded = lines.decode('utf-8', 'replace').splitlines()
                        log_event(self.directory,'terminal_ssh_notice',last_line=decoded[-1],lines=len(decoded))
                except OSError:
                    pass
                try:
                    self.sync_once()
                    # Backlog is separate from the live status poll; one older
                    # session at a time, with acknowledged byte offsets.
                    if pending and not self.stop_requested.is_set():
                        old = pending.pop(0); self.sync_once(old)
                        offsets = json.loads((old/'sync-offsets.json').read_text())
                        if any((old/n).exists() and (old/n).stat().st_size > offsets.get(n,0) for n in FILES):
                            pending.append(old)
                except Exception as exc:
                    if time.monotonic()-self.last_warning > 30:
                        log_event(self.directory, 'sync_pending', reason=str(exc))
                        self.last_warning = time.monotonic()
                self.closed.wait(2)
            try:
                self.sync_once()
            except Exception:
                pass
        self.thread = threading.Thread(target=work, daemon=True); self.thread.start()

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=10)
