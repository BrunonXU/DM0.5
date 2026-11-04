"""Terminal supervision only: readiness is process data, never screen pixels."""
import json
import os
from pathlib import Path
import subprocess
import time

READY = 'F1 RECEIVER READY v1'


def process_info(pid):
    fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), fields[19]


def ready_status(path, pane_pid, *, now=None, proc=process_info):
    """Reject stale files, reused PIDs and another session's live receiver."""
    try:
        d = json.loads(Path(path).read_text())
        age = (time.monotonic_ns() if now is None else now) - d['updated_monotonic_ns']
        if not 0 <= age < 3_000_000_000:
            return None
        pid = int(d['pid'])
        parent, ticks = proc(pid)
        if str(ticks) != str(d['start_ticks']):
            return None
        if d['status']['state'] in ('failed', 'stopping'):
            return None
        seen = set()
        while pid != pane_pid:
            if pid <= 1 or pid in seen:
                return None
            seen.add(pid)
            pid = parent
            parent, _ = proc(pid)
        return d
    except (OSError, ValueError, KeyError, TypeError):
        return None


def attach(tmux, name, child, *, status_path=None):
    def call(*args, check=True):
        return subprocess.run([tmux, *args], check=check, capture_output=True, text=True)
    exists = call('has-session', '-t', name, check=False).returncode == 0
    if not exists:
        call('new-session', '-d', '-s', name, child)
    # Session-local options; do not change the shared tmux server's defaults.
    call('set-option', '-t', name, 'status', 'off')
    call('set-option', '-t', name, 'mouse', 'off')
    if status_path is not None:
        pane = int(call('display-message', '-p', '-t', name, '#{pane_pid}').stdout.strip())
        print('[启动] 已连接 F1，正在初始化；尚未开始任务，无需 Enter。', flush=True)
        last_notice = 0.
        try:
            while call('has-session', '-t', name, check=False).returncode == 0:
                status = ready_status(status_path, pane)
                if status is not None:
                    # Outside tmux's renderer: always one complete plain line.
                    print(READY + ' ' + status['run_id'], flush=True)
                    print('[连接] 接收端已就绪，正在连接手柄；以随后控制状态为准。', flush=True)
                    break
                if time.monotonic() - last_notice >= 5:
                    print('[启动] 等待模型、相机及接收端完成检查；还不能 START。', flush=True)
                    last_notice = time.monotonic()
                time.sleep(.2)
            else:
                raise RuntimeError('F1 初始化进程已退出，请查看原始日志；未启动手柄。')
        except KeyboardInterrupt:
            # Attach to the actual terminal so the operator can inspect/exit.
            # Never inject START, Enter or kill a process from this view layer.
            print('[连接] 转入机器人终端；Ctrl+C 可结束机器人程序。', flush=True)
    # HIL with Enter must attach immediately, otherwise it cannot be authorized.
    os.execvp(tmux, [tmux, 'attach-session', '-t', name])
