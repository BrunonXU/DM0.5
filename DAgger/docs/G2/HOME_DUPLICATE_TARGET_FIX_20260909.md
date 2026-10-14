# 2026-09-09 Home重复目标超时修复

> 后续现场更新（2026-09-10）：操作者确认右拇指硬件故障并送修，本阶段暂停。本文保留为修前证据，最新状态见 [送修交接](PAUSED_HAND_REPAIR_20260910.md)。

用户执行03_reset_home后：双臂完成，首个手命令锚点确认，后续等待Split5Hz目标超时。远端logs/runtime/last_hand_command.json记录hand-home-1788942506在16:28:50.2205 SDK确认一次目标，command_index=1，positions与批准Home逐项相同。

根因：start==target时build_bounded_hand_trajectory仍返回两行相同目标。worker changed-only跳过第二次相同目标，但Home要求hand_send_count再增加，所以2秒后超时。不是用户没执行复位。该SDK确认与后续左手仍闭合的reported反馈不一致，还不能由此判定硬件原因。

修复：完全相同端点只返回一个锚点，仍等待本轮第一次实际SDK确认；不修改去重、超时阈值、Home关节目标或回放起点门禁。结束输出左右手目标/reported原始反馈及偏差；完成文案明确SDK确认不等于物理到位。原Home低延迟臂通道和5Hz手目标路径不变。

本地50项operations/replay纯逻辑测试通过，包括真实mock Split worker的相同端点和不同端点两种执行。远端源文件修改前与本地一致。新修复尚未真机验收，由用户现场启动。

另有明确限制：新34D replay控制器未写入Split last_hand_command.json；此文件不能自动当最新34D动作或真实手位置。现阶段先观察修复后的原始反馈对照，若SDK确认开手但反馈/视觉仍闭合，应核对手执行语义交接与GDK状态，而不是继续提高闭合值或跳过起点检查。

用户贴出的software emergency stop latched可能是失败清理时本进程禁用继续发送的输出，不能单凭该行判定硬件急停或跨进程锁死；最新replay报告直接失败原因仍是hand start mismatch。
