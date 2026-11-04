"""F1-only right-arm, startup and console adapters for the shared sender loop."""
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
        return ('人工：默认右臂，无需十字键选侧；START 进入人工后，释放回中再按 RB 控制右臂；'
                'RT 切换右夹爪开合；十字键左右不切换手臂；'
                'A 切换速度；Y 请求右臂积木demo-V1复位，夹爪保持。')
    if message.startswith('录制由 F1 recording-mode'):
        return ('连续录制：START 开始一条，策略中再按 START 接管；人工阶段是否交还以接收端模式回执为准。'
                '松 RB、等夹爪及可选 Y 回位完成后按十字键↑保存；保存完成再 START 下一条。'
                '短时暂停松 RB 回中再按 RB 恢复。自由接入不录制。需配套新版 F1 接收端。')
    if message.startswith('Confirm the F1 terminal'):
        return ('确认 F1 终端模式：SHADOW 只计算和记录，不输出动作；'
                'POWERED 才可能下发动作。ACK 不证明实际运动。')
    return (message.replace('退出须松 RB、等手部/Home 完成，再按 START。',
                            '松 RB、等夹爪及可选 Home 完成后↑保存；START 行为以模式回执为准。自由接入不录制。')
            .replace('人工已退出；F1 正基于新观测请求策略。', 'F1 正等待新观测与策略。')
            .replace('再选侧、释放十字键、按 RB', '默认右臂，释放回中后按 RB')
            .replace('重新选侧并释放十字键', '摇杆回中并释放十字键')
            .replace('十字键选侧并释放，再按 RB 控制臂', '释放回中，再按 RB 控制右臂')
            .replace('HIL / 未选侧', 'HIL / 右侧'))


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
    original_mapper = sender.F710Mapper
    pending = {}

    class F1RightArmMapper(original_mapper):
        """Select only F1's right arm without weakening the shared input gates."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.active_side = 'right'
            self._end_id = 0
            self._local_result = 'success'
            self._end_down = False
            self._end_released = False
            self._switch_id = 0
            self._switch_down = False
            self._switch_released = False
            self._switch_capable = False
            self._home_end_id = getattr(self, "_home_end_id", 0)
            self._home_down = self._home_released = False
            self._local_capable = False

        def reset_for_disconnect(self):
            super().reset_for_disconnect()
            self.active_side = 'right'
            self._end_down = False
            self._end_released = False
            self._switch_down = self._switch_released = False
            self._switch_capable = False
            self._home_end_id = getattr(self, "_home_end_id", 0)
            self._home_down = self._home_released = False
            self._local_capable = False

        def map(self, sample):
            # Negotiate through the existing ACK metadata. Old receivers must
            # never receive an extension they cannot decode.
            transport = pending.get('transport')
            state = transport.snapshot() if transport is not None else None
            ack = getattr(state, 'last_ack', None)
            capable = bool(getattr(state, 'connected', False) and ack is not None
                and ';rlt_gamepad_v1=1' in (ack.reason or ''))
            local_v3 = bool(capable and ';rlt_local_v3=1' in (ack.reason or ''))
            local_v4 = bool(capable and ';rlt_local_v4=1' in (ack.reason or ''))
            local_capable = bool(local_v4 or local_v3 or (capable and ';rlt_local_v2=1' in (ack.reason or '')))
            if not local_capable or local_capable != self._local_capable:
                self._home_down = self._home_released = False
            self._local_capable = local_capable
            if local_capable and sample.connected and not sample.y:
                self._home_released = True
            if (local_capable and sample.connected and sample.y and not self._home_down
                    and self._home_released and not sample.rb and not sample.start
                    and sample.dpad_x == 0 and sample.dpad_y == 0):
                self._home_end_id += 1
            self._home_down = sample.y
            if not capable or capable != self._switch_capable:
                self._switch_down = self._switch_released = False
            self._switch_capable = capable
            right = sample.connected and sample.dpad_x == 1 and sample.dpad_y == 0
            if capable and sample.connected and sample.dpad_x == 0 and sample.dpad_y == 0:
                self._switch_released = True
            if (capable and right and not self._switch_down and self._switch_released
                    and not sample.rb and not sample.start and not sample.y):
                self._switch_id += 1
            self._switch_down = right
            # Keep every D-pad press non-neutral for the original RB interlock.
            # Both horizontal directions can only reselect the default arm.
            if sample.dpad_x < 0:
                sample = replace(sample, dpad_x=1)
            # A default side must not turn a trigger already held at startup or
            # reconnect into a new gripper command. Consume that edge until the
            # original neutral check has seen all controls released.
            if not self._neutral_seen and sample.right_trigger >= self.grip_press_threshold:
                self._right_trigger_pressed = True
            up = sample.connected and sample.dpad_y == 1 and sample.dpad_x == 0
            down = local_v4 and sample.connected and sample.dpad_y == -1 and sample.dpad_x == 0
            rating = up or down
            if sample.connected and sample.dpad_y == 0:
                self._end_released = True
            if (rating and not self._end_down and self._end_released and not sample.rb
                    and not sample.start and not sample.y):
                self._end_id += 1
                self._local_result = 'failure' if down else 'success'
            self._end_down = rating
            intent = super().map(sample)
            # Y/disconnect retain the original no-motion packet and clear hand
            # latches. The next connected sample still defaults to the right.
            self.active_side = 'right'
            return replace(intent, operator_protocol=('f1_rlt_local_v4' if local_v4 else 'f1_rlt_local_v3' if local_v3 else 'f1_rlt_local_v2') if local_capable else None,
                           local_result=self._local_result if local_v4 else None,
                           home_end_id=self._home_end_id if local_capable else None, episode_end_id=self._end_id, episode_end_pressed=rating,
                           policy_switch_id=self._switch_id if capable else None,
                           policy_switch_pressed=right if capable else None,
                           warning=None if up and intent.warning == 'vertical_dpad_unassigned' else intent.warning)

    @contextmanager
    def transport_when_ready(*args, **kwargs):
        kwargs['progress_timeout_s'] = 1.0
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
        if metadata.get('rlt_local_v4') == '1' or metadata.get('rlt_local_v3') == '1' or metadata.get('rlt_local_v2') == '1':
            prefix += ('[RLT：→每条一次；↑成功／↓失败；先评分再交还或Y] '
                       if metadata.get('rlt_local_v4') == '1' else '[RLT：→每条一次；↑局部成功；Y复位并保存] ')
            if ack.accepted and ack.reason == 'powered_hil:hil_latched':
                return prefix + '人工控制保持；放好先↑，再START交回VLA；可再次START纠正撤离回位。'
        elif metadata.get('rlt_gamepad_v1') == '1':
            prefix += '[RLT：→切入；START接管/交还；↑保存] '
            if ack.accepted and ack.reason == 'powered_hil:hil_latched':
                return prefix + '人工介入：RB控制右臂；松RB、等夹爪完成后START交还当前策略。'
        demonstration = {
            'powered_hil:episode_ready': '当前未录制；整理场景后，释放控制再按 START 开始新的一条。',
            'powered_hil:episode_starting': '正在准备记录；保持控制释放。',
            'powered_hil:episode_saving': '本条正在保存；等待保存完成再按 START，提前按键不会排队。',
            'powered_hil:input_wait_release_rb': '输入短暂中断：机器人保持。松 RB、摇杆回中，等待提示后再按 RB；不用 START。',
            'powered_hil:input_wait_fresh_packet': '等待新鲜输入。松 RB、摇杆回中；恢复后再按 RB。',
            'powered_hil:input_wait_press_rb': '新鲜输入已确认，按 RB 恢复人工控制；不用 START。',
            'powered_hil:input_wait_feedback': '等待新鲜机器人反馈；保持松 RB，确认正常后再按 RB。',
            'powered_hil:input_wait_update_sender': '输入保持；需要新版 Windows sender 提供新鲜输入证明。旧版不能用 RB 恢复。',
            'powered_hil:startup_wait_operator': 'START 已受理，等待操作连接恢复；恢复后自动取新观测启动 VLA，无需重复 START。',
            'powered_hil:policy_wait_operator': '通信短暂等待，机器人保持；同会话及时恢复后重新规划。START可转人工，旧动作不会重放。',
            'powered_hil:input_wait_start_takeover': '策略已暂停；START 转人工接管，然后松 RB 回中、再按 RB 控臂。',
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
    sender.F710Mapper = F1RightArmMapper
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
