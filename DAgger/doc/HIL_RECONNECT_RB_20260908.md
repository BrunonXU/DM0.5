# 2026-09-08 最新录制：断线后 RB 无响应

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](../RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

核对运行 g2_powered_hil_20260908_161753（G2 journal、累计 console 中本轮片段、runtime_start、payload manifest）。

实际参数 hil-record / capture_hz=30 / duration_seconds=0。首次 START 只授权运动，不启动/停止录制。建议保留首次 START 及后续所有权切换，录制模式与控制权独立。

相对 runtime_waiting_for_start：

|秒|输入间隔 ms|等待 START 恢复秒|SAFE_HOLD 期间收到左侧 RB 包|
|---|---|---|---|
|27.986|1058.09|19.443|177|
|68.964|1041.45|15.286|287|
|118.371|1032.56|4.714|0|

三次均 operator_watchdog，随后 operator_session_changed，再由 START hold_to_hil 恢复。不是 RB 没到 G2；SAFE_HOLD 不允许 RB 单独恢复。日志 intervention_id 包括恢复，因此不能把用户“第二次 HIL”直接等同 intervention_id=2。

根因边界：G2 证据证明输入断流与新会话，但不能判断是 Windows sender、SSH/Wi-Fi 或手柄接收链路。需要 Windows 同时段 sender 日志，不能断言蓝牙故障。累计 console 含历史 Traceback，不能算本轮异常。本轮还存在大量 SDK Param not found 输出，未证明其导致断线。

已修复 40 脚本误导性“输入恢复后自动接管”提示，SAFE_HOLD 增加释放控制→START→选侧→RB 的说明。未修改 watchdog、执行或自动恢复语义。脚本 AST 语法检查通过；本轮 journal 离线审计 valid=true、errors=[]。仅提示变更，无新增真机验收。

本轮 journal 4261 observations / 19 chunks / 13624 control；payload manifest 4261 帧组、complete=true、drop_count=0、writer_error=null。这是保存清单，不是完整图像/hash/训练审计；training_ready=false。录制覆盖 HOLD 和断线等待。

证据：data/archive/offline/20260908_161753/journal 和 journal_audit.json；G2 原始图像仍在 data/raw/powered_hil/g2_powered_hil_20260908_161753/payload。

## Windows 日志补证与 sender 提示 v2

用户提供累计日志 pasted-text（e0078491-b1b1-4aaa-be4b-fdb4f062d249）。最新 sender 启动片段第1622行起，运行中1648/1701/1770行三次 ACK timeout，重连后均 powered_hil:safe_hold；未出现 F710 disconnected。结束后1810行起连接关闭/重试是另一个阶段，不合算运行中掉线。采样窗口 p50 16ms / p99 32ms，ACK RTT 可达469ms。不能据此直接确定 Wi-Fi、SSH、系统调度或硬件接收器是哪一层根因。

核对 transport 实现：实际超时条件是最老待确认包年龄超过500ms，并非500ms完全没有任何ACK。仅修正文案，增加 oldest_age_ms / limit_ms / pending_count / oldest_packet；保留原阈值与传输逻辑。

Windows sender 新增依据 G2 ACK 的中文状态指导：等待开始、同步策略（包括等待推理）、人工选侧/RB门控、等待新策略、SAFE_HOLD恢复、输入拒绝、未知接收端和SHADOW。START输入与远端状态确认分开；不把ACK当SDK执行/实测反馈，不推断录制状态。默认显示简要输入、原始reason及ack_seq，--verbose可查看旧详细字段。状态变化才更新指导；诊断时间带时区和毫秒，5秒统计带session/generation/last_ack_seq。连接和手柄断开提示分开。

修改文件：scripts/03_windows_shadow_sender.py、src/g2dagger/sender_guidance.py、src/g2dagger/async_transport.py，及 windows_client 同步副本和manifest。8项针对性测试通过（含本机回环网络）；起初缺PYTHONPATH及沙箱socket限制已按环境修正重跑。Windows中文显示与现场使用待用户启动，不代表断流根因已修复。
