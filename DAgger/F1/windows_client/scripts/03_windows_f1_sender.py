"""F1-only startup and console adapter around the unchanged F710 sender loop."""
from __future__ import annotations

import builtins
from contextlib import contextmanager
import ctypes
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys


class WindowsQuickEditGuard:
    """Disable legacy console selection pauses; restore the exact original mode."""
    def __init__(self, *, platform=None, kernel32=None):
        self.platform = sys.platform if platform is None else platform
        self.kernel32 = kernel32
        self.handle = None
        self.original_mode = None
        self.changed = False

    def __enter__(self):
        if self.platform != 'win32':
            return self
        try:
            if self.kernel32 is None:
                from ctypes import wintypes
                self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                self.kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
                self.kernel32.GetStdHandle.restype = wintypes.HANDLE
                self.kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                self.kernel32.GetConsoleMode.restype = wintypes.BOOL
                self.kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                self.kernel32.SetConsoleMode.restype = wintypes.BOOL
                mode = wintypes.DWORD()
            else:
                mode = ctypes.c_uint32()
            self.handle = self.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            if self.handle in (None, 0, ctypes.c_void_p(-1).value):
                return self
            if not self.kernel32.GetConsoleMode(self.handle, ctypes.byref(mode)):
                return self  # Redirected input / terminals without a Win32 console.
            self.original_mode = mode.value
            desired = (mode.value | 0x0080) & ~0x0040  # EXTENDED_FLAGS on, QUICK_EDIT off.
            self.changed = bool(self.kernel32.SetConsoleMode(self.handle, desired))
        except (OSError, AttributeError, TypeError, ValueError):
            pass  # Optional console behavior must not prevent F710 startup.
        return self

    def __exit__(self, *_args):
        if self.changed:
            try:
                self.kernel32.SetConsoleMode(self.handle, self.original_mode)
            except (OSError, AttributeError, TypeError, ValueError):
                pass
            self.changed = False


def f1_text(message):
    message = message.replace('G2', 'F1').replace('DM0.5', '已配置策略')
    if message.startswith('流程：tunnel →'):
        return ('流程：tunnel → F1 监听 → sender 模式回执 → START；'
                'SHADOW / 纯人工入口首次 START 进入人工，策略入口首次 START 开始策略。'
                '以 F1 终端的运行模式为准。')
    if message.startswith('人工：十字键'):
        return ('人工：十字键选侧并释放 → RB 控制臂；RT 切换所选夹爪开合；'
                'A 切换速度；Y 请求双臂 Home，夹爪保持。')
    if message.startswith('Confirm the F1 terminal'):
        return ('确认 F1 终端模式：SHADOW 只计算和记录，不输出动作；'
                'POWERED 才可能下发动作。ACK 不证明实际运动。')
    return message


def load_sender():
    path = Path(__file__).with_name('03_windows_shadow_sender.py')
    spec = importlib.util.spec_from_file_location('_f1_frozen_sender', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_sender(sender, quickedit=None):
    """Adapt only this loaded module; never modify builtins or G2 package globals."""
    original_transport = sender.AsyncLatestIntentTransport
    original_wait = sender.wait_for_gamepad
    original_operator = sender.operator_guidance
    original_transport_guidance = sender.transport_guidance
    original_install_log = sender.install_console_log
    original_announce = sender.announce
    pending = {}

    @contextmanager
    def transport_when_ready(*args, **kwargs):
        transport = original_transport(*args, **kwargs)
        pending.update(transport=transport, started=False)
        try:
            yield transport
        finally:
            transport.close()
            pending.clear()

    def wait_for_gamepad(reader):
        original_wait(reader)
        if not pending.get('started'):
            pending['transport'].start()
            pending['started'] = True

    def operator(intent, ack):
        if ack is None:
            return ('[等待 F1 回执] 目前只连到 TCP / 本地 SSH 隧道，尚未确认 F1 接收程序存活。'
                    '若持续无回执，请查看 F1 终端是否已报错退出及 raw 目录中的 failure.txt；'
                    '此时 START 无法获得模式确认，不据此判断手柄模式。')
        execution = None
        metadata = {}
        if ack.reason:
            reason, separator, fields = ack.reason.partition(';')
            if separator:
                metadata = dict(field.split('=', 1) for field in fields.split(';') if '=' in field)
                mode = metadata.get('execution_mode')
                if mode in ('shadow', 'powered'):
                    execution = mode
                ack = replace(ack, reason=reason)
        prefix = ({'shadow': '[F1 SHADOW 无动作输出] ',
                   'powered': '[F1 POWERED 执行入口；ACK 不证明运动] '}.get(
                       execution, '[F1 执行模式未声明；请核对 F1 终端] '))
        if metadata.get('release'):
            prefix += '[版本 '+metadata['release']+'] '
        demonstration = {
            'powered_hil:demonstration_ready': '人工示教未录制；按 START 开始新的一条。',
            'powered_hil:demonstration_starting': '人工示教准备中；保持控制释放。',
            'powered_hil:demonstration_saving': '本条正在保存；保存完成后再按 START 开始下一条。',
        }
        if ack.accepted and ack.reason in demonstration:
            return prefix + demonstration[ack.reason]
        if ack and ack.accepted and ack.reason == 'powered_hil:waiting_for_teleop_start':
            return prefix + '[F1 等待人工开始] 释放 START 后单击进入人工；不会请求模型。'
        if ack and ack.accepted and ack.reason == 'powered_hil:manual_hold':
            return prefix + '[F1 人工暂停] START 重新进入人工。'
        return prefix + f1_text(original_operator(intent, ack))

    def transport_guidance(message):
        if message.startswith('Connected generation='):
            return f1_text(original_transport_guidance(message))
        return (f'[连接异常] {f1_text(message)}\n'
                '下一步：保持 tunnel 窗口打开，先查看 F1 接收终端是否仍运行；'
                '若有 traceback，请保留 raw 路径和 failure.txt，修复接收端后再连接。'
                '本地 SSH 能连不代表远端 7906 正在监听。'
                '收到 SAFE_HOLD 回执后释放控制，再按 START；ACK 超时不证明手柄掉线。')

    def install_log(path):
        original_install_log(path)
        if quickedit is not None and quickedit.changed:
            sender.announce('F1：已关闭本控制台 QuickEdit 选中文本暂停；退出时恢复。')
        else:
            sender.announce('F1：本终端未调整 QuickEdit；避免在运行窗口选中文本，排查时读取日志文件。')
        sender.announce('F1：先初始化 F710，再连接接收端；ACK 超时与 watchdog 保持原值。')

    sender.AsyncLatestIntentTransport = transport_when_ready
    sender.wait_for_gamepad = wait_for_gamepad
    sender.operator_guidance = operator
    sender.announce = lambda message: original_announce(f1_text(message))
    sender.transport_guidance = transport_guidance
    sender.print = lambda *args, **kwargs: builtins.print(
        *(f1_text(value) if isinstance(value, str) else value for value in args), **kwargs)
    sender.install_console_log = install_log
    return sender


def main():
    with WindowsQuickEditGuard() as quickedit:
        return configure_sender(load_sender(), quickedit).main()


if __name__ == '__main__':
    raise SystemExit(main())
