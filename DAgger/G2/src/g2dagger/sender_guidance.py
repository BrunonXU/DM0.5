"""Operator instructions derived from receiver ACKs, never measured motion."""
from __future__ import annotations


def operator_guidance(intent, ack) -> str:
    if ack is None:
        return '[等待回执] TCP 已连接，尚未确认 G2 模式；保持控制释放，检查 G2 监听窗口。'
    if not ack.accepted:
        return f'[输入被拒绝] G2 原因：{ack.reason}；释放控制并检查 G2 日志。'
    reason = ack.reason or ''
    if reason.startswith('powered_hil:'):
        state = reason.split(':', 1)[1]
        if state in {'waiting_for_start', 'waiting_for_operator'}:
            return '[等待开始 START] 释放所有控制后，单击 START 开始 DAgger 策略流程。'
        if state == 'policy':
            return '[同步策略模式 DM0.5] G2 已确认 POLICY（含推理等待）；START 请求人工介入，RB 不切换模式。'
        if state == 'handoff_hold':
            return '[等待新策略] 人工已退出；G2 正基于新观测请求策略。等待即可；START 可重新进入人工。'
        if state == 'safe_hold':
            return '[安全暂停 SAFE_HOLD] RB / Y 不能恢复。连接恢复后释放控制，单击 START 重进人工，再选侧、释放十字键、按 RB。'
        if state == 'hil_latched':
            side = {'left': '左侧', 'right': '右侧'}.get(intent.active_side, '未选侧')
            if intent.warning:
                action = f'输入门控：{intent.warning}；松 RB，重新选侧并释放十字键，再按 RB。'
            elif intent.active_side not in {'left', 'right'}:
                action = '先松 RB，十字键选侧并释放，再按 RB 控制臂。'
            elif intent.deadman:
                action = '本地 RB 臂控制请求有效；实际执行还受 G2 门控，以真机和 G2 日志为准。'
            else:
                action = '按住 RB 控制臂；松 RB 仍留在人工模式。'
            return f'[人工介入 HIL / {side}] {action} RT 切换开合；退出须松 RB、等手部/Home 完成，再按 START。'
    if 'shadow' in reason.lower() or 'SHADOW' in ack.receiver_state:
        return '[SHADOW 只读] 当前用于验证输入，不会操控真机；核对 G2 启动入口。'
    return (f'[接收端 {ack.receiver_state}] reason={reason}；非已识别 Powered HIL 模式，'
            '请按对应 G2 入口说明操作，不推断 START 功能。')


def transport_guidance(message: str) -> str:
    if message.startswith('Connected generation='):
        return f'[连接建立] {message.split(".", 1)[0]}；释放控制，等待 G2 模式回执。连接成功不代表恢复运动。'
    return (f'[连接异常] {message}\n'
            '下一步：保持 tunnel 窗口打开，检查 G2 程序是否仍在监听；重连后查看模式。'
            '若为 SAFE_HOLD，释放控制后按 START 恢复人工。ACK 超时不是手柄掉线的证明。')
