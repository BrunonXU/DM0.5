import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('inference_console', SCRIPTS / 'inference_console.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
WARN = "[WARN] [1792548000.1234] [rmw_cyclonedds_cpp]: Failed to parse type hash for topic 'rt/rosout' with type 'rcl_interfaces::msg::dds_::Log_' from USER_DATA '(null)'."


def test_exact_allowlist_unknown_warning_and_failures():
    out=io.StringIO(); c=mod.Console(out)
    for _ in range(20): c.line(WARN)
    c.line('[WARN] unknown new warning')
    c.line(WARN.replace("'(null)'", "'different'"))
    c.line('RuntimeError: camera feedback stale')
    c.finish(); s=out.getvalue()
    assert c.dds==20 and s.count('类型哈希缺失警告已合并')==1
    assert 'unknown new warning' in s and "'different'" in s and 'camera feedback stale' in s


def test_status_retains_errors_drops_hil_pause():
    out=io.StringIO(); c=mod.Console(out)
    c.line('F1 STATUS '+json.dumps(dict(state='safe_hold',hold_reason='ik_paused',warning='joint stale',recording=dict(writer_error='disk full',drop_count=2),cameras=dict(error='head missing',drop_counts=dict(head=4)))))
    c.line('HIL故障保持，按START进入人工接管')
    s=out.getvalue()
    for v in ('ik_paused','joint stale','disk full','head missing','recording','START'):assert v in s
    assert 'Enter' not in s


def test_policy_boundary_pause_is_not_reported_as_manual_control():
    out=io.StringIO(); c=mod.Console(out)
    c.status(dict(state='safe_hold', hold_reason='input_wait_start_takeover', rlt_phase='base'))
    assert '策略已暂停 · 等待 START 人工接管' in out.getvalue()
    assert '[状态] 人工接管' not in out.getvalue()
    c.status(dict(state='hil_latched', hold_reason=None, rlt_phase='base'))
    assert '[状态] 人工接管 · B1250阶段' in out.getvalue()


def test_verification_failure_and_unknown_json_preserved():
    out=io.StringIO();c=mod.Console(out)
    for l in json.dumps(dict(ok=False,errors=['motor_ctl_node=0'],control_publishers_created=False,joint_configuration={}),indent=2).splitlines(True):c.line(l)
    c.line('{"unexpected": "retain me"}')
    c.line('F1 STATUS invalid JSON')
    c.finish();s=out.getvalue()
    assert 'motor_ctl_node=0' in s and '未创建' in s and 'retain me' in s and 'invalid JSON' in s
    assert '✓' not in s


def test_does_not_report_unsaved_as_saved():
    out=io.StringIO();c=mod.Console(out)
    c.line('F1 SESSION '+json.dumps(dict(run_id='x',phase='failed',errors=['flush failed'])))
    assert 'flush failed' in out.getvalue() and '已保存' not in out.getvalue()


def read_until(fd, needle, timeout=8):
    data=b''; deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if select.select([fd],[],[],.1)[0]:
            try: chunk=os.read(fd,65536)
            except OSError:break
            data+=chunk
            if needle in data:return data
    raise AssertionError(data.decode(errors='replace'))


def run_pty(tmp_path, interrupt):
    raw=tmp_path/'raw.log'; fake=tmp_path/'fake.py'
    fake.write_text('''import sys,signal,time
assert sys.stdin.isatty(), 'operator stdin lost TTY'
print("TTY_OK",flush=True)
input('F1 sync: confirm scene and physical emergency stop ready; press Enter to begin. ')
print('AUTHORIZED_ONCE',flush=True)
'''+('''try:
    time.sleep(30)
except KeyboardInterrupt:
    print('CLEANUP_AFTER_SIGINT',flush=True)
    sys.exit(23)
''' if interrupt else 'sys.exit(17)\n'))
    shell=tmp_path/'start.sh';shell.write_text('set -eo pipefail\nsource "$1/inference_console.sh"\nf1_console_run "$2" "$3" "$4"\n')
    pid,fd=pty.fork()
    if pid==0:
        os.execv('/bin/bash',['bash',str(shell),str(SCRIPTS),str(raw),sys.executable,str(fake)])
    try:
        output=read_until(fd,'按 Enter 开始动作'.encode())
        assert b'AUTHORIZED_ONCE' not in output
        os.write(fd,b'\n')
        output+=read_until(fd,b'AUTHORIZED_ONCE')
        if interrupt:
            os.write(fd,b'\x03')
            output+=read_until(fd,b'CLEANUP_AFTER_SIGINT')
        deadline=time.monotonic()+8;status=None
        while time.monotonic()<deadline:
            w,status=os.waitpid(pid,os.WNOHANG)
            if w:break
            time.sleep(.05)
        else:raise AssertionError('pipeline failed to exit')
        assert os.waitstatus_to_exitcode(status)==(23 if interrupt else 17)
        text=raw.read_text();assert text.count('AUTHORIZED_ONCE')==1 and 'TTY_OK' in text
        assert 'press Enter to begin.' in text
        if interrupt:assert 'CLEANUP_AFTER_SIGINT' in text
    finally:
        try:os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass
        os.close(fd)


def test_pty_enter_and_original_exit_code(tmp_path):run_pty(tmp_path,False)
def test_pty_ctrl_c_reaches_control_and_preserves_cleanup(tmp_path):run_pty(tmp_path,True)


def test_shadow_never_claims_a_saved_recording():
    out=io.StringIO();c=mod.Console(out)
    c.line('F1 SHADOW standalone sync: duration=5; Ctrl+C stops; recording=off')
    c.line('F1 SHADOW END '+json.dumps(dict(recording=False,status='completed',submitted=0,published=0,errors=[])))
    s=out.getvalue()
    assert '不录制' in s and '未录制' in s and '会话目录' not in s and 'saved' not in s
    c.line('F1 SHADOW END '+json.dumps(dict(recording=False,status='failed',submitted=1,published=0,errors=['cleanup failed'])))
    assert 'cleanup failed' in out.getvalue() and '输出计数非零' in out.getvalue()


def test_phase_switch_is_visible_even_with_unchanged_policy_state():
    out=io.StringIO();c=mod.Console(out)
    c.status({'state':'policy','rlt_phase':'base'})
    c.status({'state':'policy','rlt_phase':'actor'})
    c.status({'state':'hil_latched','rlt_phase':'actor'})
    text=out.getvalue()
    assert '[状态] 策略执行 · B1250阶段' in text
    assert '[状态] 策略执行 · RLT actor阶段' in text
    assert '[状态] 人工接管 · RLT actor阶段' in text


def test_waiting_distinguishes_receiver_and_real_gamepad():
    out=io.StringIO();c=mod.Console(out)
    c.status(dict(state='episode_ready',input_fresh=False,session=None,rlt_phase='base'))
    assert '手柄未连接' in out.getvalue() and '策略执行' not in out.getvalue()
    c.status(dict(state='episode_ready',input_fresh=True,session='actual-handshake',rlt_phase='base'))
    assert '手柄已连接，等待 START' in out.getvalue()
    assert '尚未开始录制' in out.getvalue()


def test_control_version_uses_loaded_identity_and_does_not_guess_missing_release():
    out=io.StringIO();c=mod.Console(out)
    c.line('F1 IDENTITY '+json.dumps(dict(release='f1-rtc-rlt-V42',release_manifest_sha256='ab'*32)))
    assert 'F1 RTC＋局部RLT V42 · 清单 abababababab' in out.getvalue()
    assert '运行端实际加载' in out.getvalue()
    c.line('F1 IDENTITY '+json.dumps(dict(release='f1-rtc-rlt-V43',release_manifest_sha256='cd'*32)))
    assert c.control_version == 'F1 RTC＋局部RLT V43 · 清单 cdcdcdcdcdcd'
    c.line('F1 IDENTITY {}')
    assert c.control_version == '未知 · 清单 未知／开发源码'


def test_actor_version_is_shown_once_per_real_segment_not_health_or_each_status():
    out=io.StringIO();c=mod.Console(out)
    state=dict(state='policy',rlt_phase='base',actor_version='bootstrap-bc400',checkpoint_step=400)
    c.status(state)
    assert 'RLT版本' not in out.getvalue()
    state.update(rlt_phase='actor',rlt_segment_id='first:placement:1',
                 actor_version='adopted-q1024',checkpoint_step=1024,q_guidance_updates=256)
    c.status(state);c.status(state)
    assert out.getvalue().count('RLT版本') == 1
    assert 'adopted-q1024 · 训练step 1024 · Q更新 256 · 本局部段固定' in out.getvalue()
    state.update(rlt_segment_id='next:placement:1',actor_version='adopted-q1088',checkpoint_step=1088)
    c.status(state)
    assert out.getvalue().count('RLT版本') == 2
    state.update(rlt_segment_id='unknown:placement:1',actor_version=None,checkpoint_step=None,q_guidance_updates=None)
    c.status(state)
    assert '未知（本次状态未提供） · 训练step 未知 · Q更新 未知' in out.getvalue()
