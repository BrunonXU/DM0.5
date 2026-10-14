# Windows → G2 零输出HIL现场验证

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

当前门：物理START已由Windows原始probe确认是button7（六对DOWN/UP）。本轮新增入口37，
验证真实reader/mapper、现有IntentPacket与网络、G2 HILSupervisor。**未初始化GDK、没有相机、
没有IK、没有Robot/Servo/Home，不是Powered HIL，不是训练episode。**

## 入口与隔离

- G2：`scripts/37_g2_hil_network_shadow.sh`，仅ENTER确认，监听`127.0.0.1:7906`。
- Windows：`hil_shadow_candidate/`独立目录，复用现有sender与必需模块，不覆盖旧Windows遥操目录。
  `38_prepare_windows_hil_shadow.py`从本地当前代码逐文件复制，并写明文件来源校验manifest；无安装、无压缩包。
- 7904仍留给现有遥操。不要将候选sender指向Powered receiver，也不要同时开两个F710 sender。
- 旧Windows读按钮脚本曾不显示START，现入口37会明确拒绝缺少hil_toggle_id/button字段的旧sender。

## 现场启动

先Ctrl-C停止原始按钮probe及旧sender，保持经典Windows Console Host。

G2终端运行：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/37_g2_hil_network_shadow.sh
```

按ENTER，确认`ZERO OUTPUT HIL listening 127.0.0.1:7906`。若端口占用，保留报错，不杀未知进程。

Windows下载一次独立候选（可从任一PowerShell执行）：

```powershell
scp -r agi@10.130.86.13:/home/agi/xza/G2Teleop/hil_shadow_candidate "$HOME\G2Teleop"
```

两个经典Windows PowerShell窗口分别运行：

```powershell
cd $HOME\G2Teleop\hil_shadow_candidate
.\scripts\windows_hil_shadow_tunnel.cmd
```

```powershell
cd $HOME\G2Teleop\hil_shadow_candidate
.\scripts\windows_hil_shadow_sender.cmd
```

## 操作顺序与预期

1. 启动时松开按键，先选左侧并释放十字键，再按/松RB。G2保持`policy`，没有RB快速接管。
2. 单击START进入`hil_latched`；按RB显示`human_arm`，松开显示`human_hold`但仍在HIL。
3. RB松开后换右侧，单击RT。显示`synthetic_hand_intent`与`human_hand`；模拟一秒后回human_hold。
4. RB按住时单击START，显示`hil_exit_rejected / release_rb_before_policy_handoff`。
   释放RB后再单击START；如果模拟手动作未结束，显示wait_for_human_hand_motion，需要动作结束后重新单击。
5. 正常START退出显示`handoff_hold`，随后新synthetic_observation绑定synthetic_policy_request，约一秒后
   synthetic_policy_response与`fresh_policy_to_policy`。这证明模拟request关联，不证明真实相机或模型恢复。
6. 可选迟到检查：在上述一秒HOLD中再按START，应回HIL，旧response显示policy_chunk_rejected。
7. sender窗口Ctrl-C，G2进入safe_hold；重开sender后仍safe_hold。再次START仅进入HIL，不直接恢复policy。

结束时依次Ctrl-C sender、隧道、G2 receiver。G2最后打印`Finalized:`后保存路径并提供日志；
若网络短暂停顿导致SAFE_HOLD，不跳过日志或把它算通过，按状态机操作并保留证据。

## 日志与证据边界

- G2：`logs/hil_shadow/hil_network_<YYYYMMDD_HHMMSS>_<pid>.jsonl`，运行中为.partial；Ctrl-C正常封口。
- Windows候选：`hil_shadow_candidate/logs/windows_sender_console.log`。
- 每条G2记录有sequence、host monotonic、hardware_output=false、training_ready=false。记录收到的完整
  operator_packet与ACK接受结果，状态边沿、synthetic_observation/request/response、模拟手意图。
- 观测按诊断loop中约30Hz持续生成，错过不追赶；不是严格相机采样率。policy在HIL暂停，而这些模拟
  观测继续。模拟一秒手动作不是O10实际完成反馈；Y仅记录home_ignored_zero_output，不运行Home。
- 诊断日志同步写盘，入口不承诺100Hz控制周期，也不评估实际执行owner的实时性能。
- 该日志属于network-shadow诊断schema，不应交给34的训练journal审计器。实际上下游字段由本轮
  loopback集成测试和现场日志评估；最终真实policy切换仍须采用同一个Robot owner。

## 离线验证与未决项

新增纯状态测试覆盖START/RB、handoff fresh关联、RT无RB、watchdog/重连、迟到response拒绝。
实际socket测试调用现有F710Mapper和Windows sender的make_packet，经TCP/ACK与入口37完成切换。
Linux测试的GamepadSnapshot为合成输入，不替代Windows pygame现场验证。脚本help、Python导入、
Shell语法、隔离sender依赖检查通过；未修改冻结控制文件。

本地完整回归：139 passed / 6 skipped；证据`logs/offline/20260907_hil_network/pytest.txt`。
当前未进行现场Windows→G2操作，也没有Powered结果；本轮只准备与同步入口。后续仍需真实共享
采集、设备时间域/反馈质量、policy双17D与F710 changed-only在同owner的执行语义交接。

G2同步后验证：11 passed（5项network-shadow含本机临时端口TCP联调，6项supervisor），Shell语法和
隔离sender的help/导入通过。证据`logs/offline/20260907_hil_network/g2_validation.txt`。
该自动测试只用G2本机合成输入，未启动等待现场手柄的7906 receiver，不能称Windows全链已验收。

## 首次Windows现场日志快照（2026-09-07 14:54启动）

用户确认执行Shadow后，直接从G2读取`hil_network_20260907_145407_1745462.jsonl.partial`，
本地保存为`logs/offline/20260907_hil_network/live_snapshot_145407.jsonl.partial`。
快照13403条可解析记录，sequence连续，4150条operator_packet、零拒绝；9196条模拟观测。
文件仍为partial、无runtime_stop，不把活动文件快照称为完整封口验收。

已观察到两次START进入HIL（一次由SAFE_HOLD，一次由POLICY），HIL内多次RB按/松在
human_arm/human_hold之间切换而不退出；两次START退出进入HANDOFF_HOLD，分别基于退出后
约25.462ms、0.577ms生成的模拟新观测请求，约1秒后接受模拟policy返回并恢复POLICY。
重连时保持SAFE_HOLD，随后START才进入HIL；所有记录hardware_output=false、training_ready=false。

另有两次operator_watchdog、两次operator_disconnected和一次operator_session_changed；
尚未结合Windows日志判断200ms无包是否来自操作者停止sender或链路停顿，不宣称全程无断链。
没有synthetic_hand_intent、hil_exit_rejected或迟到policy拒绝事件，因此RT、退出互锁和迟到响应的
现场覆盖仍缺失。此结果只支持已观察到的真实按钮/网络切换，观测与policy仍为模拟，机器人不动作。
本次仅获取日志、分析和更新文档，没有启动/停止任何远端进程，也没有新增测试或Powered操作。

## 首轮完整封口结论

已读取同名`.jsonl`完整文件（G2原件保留），runtime_stop.complete=true：15694条记录、sequence
连续、462.094秒总运行时长；该时长含等待连接和sender停止后的空闲，并非持续输入时长。
4150条operator_packet全部接受，114条POLICY中RB有效包未引发接管；HIL内15次进入human_arm，
释放RB仍留HIL。两次handoff分别绑定退出后25.462ms、0.577ms的模拟观测，约1000.331ms、
1007.515ms后恢复policy，时间关联成立。所有记录零硬件输出，training_ready=false。
分析产物：`logs/offline/20260907_hil_network/hil_network_20260907_145407_1745462.analysis.json`。

两次watchdog分别在最后包后204.690ms、203.048ms触发；相邻输入包间隔约72.915秒、757.615ms。
前者说明存在长时间无包，后者发生在后续断开前；只有接收端日志无法确定Windows sender暂停、
操作者停止、隧道或调度的具体原因，不能概括为随机网络卡顿，也不能忽略后宣布链路零故障。
正常Ctrl-C增加operator_stopped_receiver事件，不能误计为新的运行中watchdog。

本轮可确认START锁存、RB不改变所有权、退出HOLD/模拟fresh恢复、重连不自动恢复。
RT、RB按住时拒绝退出、手动作未完成时拒绝退出、HOLD期间再START后迟到response被拒绝，
尚无现场事件证据。下一步使用相同7906 Shadow入口补这些短测试；随后推进真实共享相机/state与
policy无输出接入。此前通过的冻结F710不能直接作为新HIL执行交接的Powered放行证据。
本次只读取和分析日志、更新文档，没有新测试运行或硬件操作。
