#!/usr/bin/env python3
"""Display-only F1 console. Input is stdout/stderr, NEVER the operator terminal.

Raw bytes are saved by upstream tee. Unknown messages pass through. No ROS,
control, stdin forwarding, policy requests, environment warning suppression.
"""
import argparse
import codecs
import json
import os
from pathlib import Path
import re
import select
import signal
import sys
import time
from urllib.request import urlopen

DDS = re.compile(r"^\[WARN\] \[[\d.]+\] \[rmw_cyclonedds_cpp\]: Failed to parse type hash for topic '[^']+' with type '[^']+' from USER_DATA '\(null\)'\.$")
PROMPTS = {
    'F1 upper16: local operator ready. Press Enter, then use F710 START to begin. ': '确认现场与急停就绪；Enter 授权，随后真实手柄 START 开始',
    'F1 sync: confirm scene and physical emergency stop ready; press Enter to begin. ': '确认现场与急停就绪；按 Enter 开始动作 · Ctrl+C 停止',
    'F1 POWERED: confirm scene and physical emergency stop ready; press Enter to arm. ': '确认现场与急停就绪；Enter 授权，随后真实手柄 START 开始',
}


def option(args, name, default=None):
    for i, value in enumerate(args):
        if value == name and i + 1 < len(args):
            return args[i + 1]
        if value.startswith(name + '='):
            return value.split('=', 1)[1]
    return default


class Console:
    def __init__(self, out=sys.stdout, raw_log='', clock=time.monotonic):
        self.out, self.raw_log, self.clock = out, raw_log, clock
        self.tty = out.isatty()
        self.row = False
        self.json_lines = []
        self.dds = 0
        self.started = None
        self.last_state = None
        self.last_status = None
        self.last_plain = 0
        self.last_drops = {}
        self.run = None
        self.control_version = None
        self.last_actor_segment = None

    def clear(self):
        if self.row:
            self.out.write('\r\033[2K')
            self.row = False

    def say(self, message):
        self.clear()
        self.out.write(message + '\n')
        self.out.flush()

    def banner(self, args):
        config = option(args, '--config')
        self.say('F1 积木分拣 · 推理控制台')
        self.say('控制版本  等待运行端完成身份校验；下方显示实际加载版本')
        self.say('─' * 48)
        try:
            cfg = json.loads(Path(config).read_text())
            policy = cfg['policy']
            # Session overlays must have the same display precedence as CLI.
            session = option(args, '--session-config')
            task = option(args, '--task', '')
            if session:
                overlay = json.loads(Path(session).read_text())
                policy = dict(policy, **overlay['policy']); task = overlay.get('task', task)
            model = policy['model_id']; location = policy.get('endpoint', '未配置')
            display = json.loads(os.environ.get('F1_INFERENCE_DISPLAY', '{}'))
            if display.get('model_id') == model and display.get('endpoint') == location:
                model = f"{display['label']} [{model}]"
                location = display['location']
            self.say('模型      ' + model)
            self.say('服务      ' + location)
            rlt = '--rlt-hil' in args
            rtc = '--rtc' in args or '--rtc-hil' in args
            hil = '--rtc-hil' in args or rlt
            mode = '真机' if '--execute' in args else '预检' if 'preflight' in args else 'SHADOW 零输出'
            duration = 'continuous' if '--continuous' in args else option(args, '--duration-seconds', '默认时限（以运行回执为准）')
            self.say(f'运行      {mode} / {duration}' + (' / 手柄HIL' if hil else ' / 独立策略'))
            pixels = policy.get('rtc_pixels_directory')
            contract = json.loads((Path(pixels) / 'contract.json').read_text()) if pixels else {}
            if contract:
                cameras = contract.get('active_cameras', contract.get('cameras', []))
                names = {'head': '主视角', 'left_wrist': '左腕', 'right_wrist': '右腕'}
                self.say('输入      ' + ' + '.join(names.get(c, c) for c in cameras) + f' · {contract.get("image_size", "?")}×{contract.get("image_size", "?")}')
                roi = contract.get('head_roi_xyxy_normalized')
                self.say('主图ROI   ' + ('A' if roi == [0.24, 0.04, 0.96, 1.0] else str(roi)))
            self.say('动作      ' + ('右臂 + 右夹爪 · 相对EEF（配置绑定）' if str(policy.get('transport', '')).startswith('eef_') else '按当前动作合同执行'))
            if rlt:
                self.say(f'B阶段     RTC D{option(args,"--rtc-delay-steps","默认")} / S≥{option(args,"--rtc-min-steps","默认")}；十字键→切入actor')
                actor_policy = cfg.get('rlt_policy', {})
                if actor_policy.get('rlt_rtc'):
                    self.say(f'RLT阶段   RTC C16 / D{actor_policy.get("rlt_rtc_delay_steps",6)} / S≥{actor_policy.get("rlt_rtc_min_steps",8)}；边执行边推理，保留已承诺前缀')
                else:
                    self.say('RLT阶段   C16取样约0.53秒，完成平滑收尾后再取新观测；START随时接管，每条仅一次')
                self.say('RLT按键   START接管／交回VLA；↑局部成功（人工保持控制）；Y复位并封存')
            elif rtc:
                self.say(f'RTC       D{option(args,"--rtc-delay-steps","默认")} / S≥{option(args,"--rtc-min-steps","默认")} 自适应 · 模型网格{policy.get("frequency_hz","?")}Hz')
            else:
                self.say(f'同步      每次执行{policy.get("chunk_steps","?")}步 · 模型网格{policy.get("frequency_hz","?")}Hz')
            recovery = 'IK实验：同观测重采样 / 可行前段恢复' if any('ik_tracking_recovery' in a for a in args) else ('HIL：策略故障START接管；人工暂停松RB回中后再RB' if hil else 'standard：IK有限重试，耗尽后暂停')
            self.say('恢复      ' + recovery)
            try:
                with urlopen(policy['endpoint'] + '/health', timeout=1.5) as response:
                    health = json.load(response)
                if health.get('model_id') == policy['model_id'] and health.get('ready') is True:
                    self.say(f'模型状态  已就绪 · H{health.get("prediction_horizon","?")} · 去噪{health.get("denoise_steps","?")}步')
                else:
                    self.say('[注意] 服务未就绪或模型身份不匹配；启动核心将继续严格校验')
            except Exception:
                self.say('[提示] 暂未取得服务health，等待原入口连接检查')
            if rlt:
                actor = cfg['rlt_policy']
                try:
                    with urlopen(actor['endpoint'] + '/health', timeout=1.5) as response:
                        health = json.load(response)
                    if (health.get('model_id') == actor['model_id'] and health.get('ready') is True
                            and health.get('model_version') == actor['model_version']):
                        update_mode = "下一次→采用通过检查的候选，段内固定" if actor.get("allow_boundary_updates") else "固定参数"
                        self.say(f'Actor状态 已就绪 · C{health.get("execute_steps", "?")} · {update_mode}')
                        self.say('Actor版本 在→受理时显示实际锁定快照；启动health不代表该段采用版本')
                    else:
                        self.say('[注意] Actor未就绪或身份不符；启动核心将继续严格校验')
                except Exception:
                    self.say('[提示] 暂未取得actor health，等待原入口连接检查')
            if 'return the right arm to the starting pose' in task:
                task = '抓取 → 插入匹配开口 → 松爪 → 张爪回起始姿态'
            elif task == 'Use the right gripper to pick up the wooden block and insert it into the matching opening of the container.':
                task = '抓取 → 插入匹配开口（旧版任务文本）'
            self.say('任务      ' + (task or '以原入口任务配置为准'))
        except Exception as exc:
            self.say('[显示配置未读取] ' + str(exc) + '；原启动校验与错误继续显示')
        self.say('原始日志  ' + self.raw_log)
        self.say('─' * 48)

    def status(self, d):
        state, reason = d.get('state'), d.get('hold_reason')
        if state == 'policy' and self.started is None:
            self.started = self.clock()
        phase = d.get('rlt_phase')
        segment = d.get('rlt_segment_id')
        if phase == 'actor' and segment and segment != self.last_actor_segment:
            self.say('RLT版本   ' + str(d.get('actor_version') or '未知（本次状态未提供）')
                     + ' · 训练step ' + str(d.get('checkpoint_step') if d.get('checkpoint_step') is not None else '未知')
                     + ' · Q更新 ' + str(d.get('q_guidance_updates') if d.get('q_guidance_updates') is not None else '未知')
                     + ' · 本局部段固定')
            self.last_actor_segment = segment
        waiting = state == 'episode_ready'
        connected = bool(d.get('input_fresh') and d.get('session'))
        key = (state, reason, phase, connected if waiting else None)
        if key != self.last_state or (waiting and self.clock() - self.last_plain >= 10):
            labels = {'policy': '策略执行', 'handoff_hold': '保持 / 等待重新规划', 'safe_hold': '保持 / 已暂停', 'human': '人工接管', 'manual': '人工接管', 'hil_latched': '人工接管', 'stopping': '正在停止', 'failed': '故障停止'}
            phase_text = {'base': 'B1250阶段', 'actor': 'RLT actor阶段'}.get(phase)
            label = ('手柄已连接，等待 START · 尚未开始录制' if connected else
                     '手柄未连接或输入已过期 · 尚未开始任务') if waiting else labels.get(state, str(state))
            if state == 'safe_hold' and reason == 'input_wait_start_takeover':
                label = '策略已暂停 · 等待 START 人工接管'
            self.say('[状态] ' + label + (f' · {phase_text}' if phase_text else '') + (f' · {reason}' if reason else ''))
            self.last_state = key
            if waiting:
                self.last_plain = self.clock()
        recording, cameras = d.get('recording') or {}, d.get('cameras') or {}
        for label, value in [('告警', d.get('warning')), ('写盘失败', recording.get('writer_error')), ('相机异常', cameras.get('error')), ('录制拒绝', recording.get('last_rejection_reason'))]:
            if value:
                self.say(f'[{label}] {value}')
        drops = dict(cameras.get('drop_counts') or {})
        drops['recording'] = recording.get('drop_count', 0)
        changed = {k: v for k, v in drops.items() if v > self.last_drops.get(k, 0)}
        if changed:
            self.say('[丢帧累计] ' + json.dumps(changed, ensure_ascii=False))
        self.last_drops = drops
        age = d.get('observation_age_ms')
        age_text = f'{age:.0f}ms' if isinstance(age, (int, float)) else '未知'
        elapsed = max(0, int(self.clock() - self.started)) if self.started is not None else 0
        row = f'运行 {elapsed//60:02d}:{elapsed%60:02d} │ 观测年龄 {age_text} │ 已写 {recording.get("completed", "?")} │ 写盘排队 {recording.get("pending_items", "?")}'
        if phase:
            row += ' │ ' + {'base': 'B1250', 'actor': 'RLT actor'}.get(phase, phase)
        # Never overwrite a pause/recovery prompt; event lines remain in scrollback.
        if state == 'policy' and not reason:
            if self.tty:
                self.clear(); self.out.write('\r' + row); self.out.flush(); self.row = True
            elif self.clock() - self.last_plain >= 10:
                self.say(row); self.last_plain = self.clock()
        self.last_status = d

    def line(self, line):
        text = line.rstrip('\r\n')
        if text.startswith('[SafetyModules] 安全配置加载成功: '):
            self.say('安全模块  正在加载配置（完整路径见日志）'); return
        if text in ('[SafetyModules] CoMGuard 初始化完成', '[SafetyModules] CollisionGuard 初始化完成'):
            return
        if text == '[SafetyModules] CombinedSafetyGuard 初始化完成（CoM + Collision）':
            self.say('✓ 重心与碰撞保护模块初始化完成'); return
        if re.fullmatch(r'\[模型\] MJCF: .+ \| 隐藏碰撞=\d+ 插入视觉=\d+', text):
            self.say('模型资源  MJCF已登记（完整路径见日志）'); return
        if DDS.fullmatch(text):
            self.dds += 1
            if self.dds == 1:
                self.say('[提示] DDS类型哈希缺失警告已合并；不表示通信已验证，原文保留在日志')
            return
        if self.json_lines or text == '{':
            self.json_lines.append(line)
            try:
                obj = json.loads(''.join(self.json_lines))
            except ValueError:
                if len(self.json_lines) < 512 and sum(map(len, self.json_lines)) < 131072:
                    return
                self.say(''.join(self.json_lines).rstrip()); self.json_lines = []; return
            original = ''.join(self.json_lines).rstrip(); self.json_lines = []
            if isinstance(obj, dict) and 'ok' in obj and 'control_publishers_created' in obj and 'joint_configuration' in obj:
                if obj['ok'] is True:
                    self.say('✓ 机器与驱动启动合同通过（相机和模型由后续入口继续检查）')
                else:
                    self.say('[启动失败] ' + '; '.join(map(str, obj.get('errors', []))))
                    self.say('动作发布端：' + ('未创建' if obj.get('control_publishers_created') is False else '请检查原始回执'))
            else:
                self.say(original)
            return
        for prefix in ('F1 STATUS ', 'F1 IDENTITY ', 'F1 SESSION ', 'F1 SHADOW END '):
            if text.startswith(prefix):
                try:
                    obj = json.loads(text[len(prefix):])
                    if prefix == 'F1 SHADOW END ':
                        self.say('[结束] SHADOW · 未录制 · ' + str(obj.get('status', '未知')))
                        if obj.get('errors'): self.say('[退出异常] ' + json.dumps(obj['errors'], ensure_ascii=False))
                        if obj.get('submitted') or obj.get('published'): self.say('[异常] SHADOW输出计数非零：' + json.dumps(obj, ensure_ascii=False))
                    elif prefix == 'F1 STATUS ': self.status(obj)
                    elif prefix == 'F1 IDENTITY ':
                        release = str(obj.get('release') or '未知')
                        match = re.fullmatch(r'f1-rtc-rlt-(V\d+)', release)
                        label = 'F1 RTC＋局部RLT ' + match.group(1) if match else release
                        manifest = obj.get('release_manifest_sha256')
                        identity = str(manifest)[:12] if manifest else '未知／开发源码'
                        self.control_version = label + ' · 清单 ' + identity
                        self.say('控制版本  ' + self.control_version + '（运行端实际加载；完整身份见原始日志）')
                    else:
                        self.say('[会话] ' + str(obj.get('phase', '未知')) + ' · ' + str(obj.get('run_id', '未知')))
                        if obj.get('errors'): self.say('[保存异常] ' + json.dumps(obj['errors'], ensure_ascii=False))
                        if obj.get('path'): self.say('记录目录  ' + str(obj['path']))
                    return
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass  # Malformed/unrecognized diagnostics must remain visible.
        if 'standalone sync:' in text and text.startswith('F1 SHADOW ') and 'recording=off' in text:
            self.started = self.clock()
            self.say('[开始] SHADOW 零输出 · 不录制'); return
        if 'standalone sync:' in text and text.startswith('F1 ') and 'raw=' in text:
            self.started = self.clock(); self.run = text.split('raw=', 1)[1]
            self.say('[开始] ' + ('SHADOW 零输出' if 'SHADOW' in text else '真机策略'))
            self.say('会话目录  ' + self.run); return
        if text.startswith('F1 sync stopped: '):
            reason = text.split(': ', 1)[1]
            self.say('[停止] ' + {'local_stop': '用户停止', 'duration_elapsed': '达到设定时长'}.get(reason, reason)); return
        if 'IK连续拒绝' in text or 'RTC响应超时' in text:
            self.say('[暂停] ' + text); return
        # Unknown warnings, tracebacks, HIL instructions and diagnostics pass through.
        self.say(text)

    def finish(self):
        if self.json_lines:
            self.say(''.join(self.json_lines).rstrip()); self.json_lines = []
        self.clear()
        if self.dds: self.say(f'本次合并DDS类型哈希警告 {self.dds} 条；完整原文见日志')
        self.say('原始日志  ' + self.raw_log)


def stream(console, fd):
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    pending = ''
    while True:
        ready, _, _ = select.select([fd], [], [], .1)
        if not ready:
            # Python input() writes no newline. Display immediately, never read TTY.
            if pending and not console.json_lines:
                for old, new in PROMPTS.items():
                    if pending == old:
                        console.say(new); pending = ''; break
                else:
                    if ('press enter' in pending.lower() or '按 Enter' in pending) and not pending.startswith('F1 STATUS '):
                        console.say(pending); pending = ''
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            pending += decoder.decode(b'', final=True)
            if pending: console.line(pending)
            break
        pending += decoder.decode(chunk)
        while '\n' in pending:
            line, pending = pending.split('\n', 1); console.line(line + '\n')
        if len(pending) > 131072:
            console.say(pending); pending = ''
    console.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-log', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    # Foreground SIGINT still reaches the original control process. Drain its
    # cleanup output; don't exit first and break the stdout pipe.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    console = Console(raw_log=args.raw_log)
    console.banner(args.command)
    stream(console, sys.stdin.fileno())


if __name__ == '__main__': main()
