# 数据集单34D回放入口（2026-09-09）

> 后续现场更新（2026-09-10）：操作者确认右拇指硬件故障并送修，本阶段暂停。本文保留为修前证据，最新状态见 [送修交接](PAUSED_HAND_REPAIR_20260910.md)。

## 用途与真实合同

G2Teleop `scripts/43_g2_dataset_unified34_replay.sh` → G2 `/home/agi/xza/DM0.5/g2_sort/operations/hardware_tests/training_data_replay.py`（6000D对应g2_robot，不是训练目录DM0.5）。

- 冻结episode sorting_20260409161901，training_replay_v1.json，948×34 **action**，30Hz，31.567秒。
- ServoStreamer按monotonic时间线性插值到100Hz，G2RobotController选择unified-34分支，每次send_action调用一次joint_servo_control，34个canonical名称与位置一一配对。
- 顺序：左臂7、右臂7、左手10、右手10。normal，有碰撞保护；无双17D拆分、无move_ee_pos、无模型、无Pink/Ruckig。源手部目标不强制二值化、不加大角度。
- 34D发送与tick迟到超过200ms仅告警，仍保留SDK非零、起点、跟踪、播放deadline等检查。日志要用于确认真实时序，100Hz为目标频率。
- 默认仅files-only，不导入GDK；--execute才进入GDK与现场ENTER倒计时。进程持有共享PID锁，拒绝另一个owner，退出释放。
- 双臂起点必须接近数据集frame0；43入口开启--prepare-hands：手部不匹配时，ENTER后同owner单34D normal保持双臂、用5秒渐进到开手起点，再等最多2秒核对原有.08rad手容差及.05rad臂容差。未通过不进入数据回放。统一34D拒绝全零/近零手反馈，不再把frame0命令冒充物理起点。没有增加数据集大跳变门限。

旧32是源速度臂state Q-direct＋手action 5Hz点目标；30/31还涉及历史加工artifact。它们均不等于本入口的原34D action回放，保留以复核历史。

## 现场命令

停止DAgger/普通遥操等其他owner。在Windows PowerShell先登录：

```powershell
ssh agi@10.130.86.13
```

进入G2 Linux终端后：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/43_g2_dataset_unified34_replay.sh --execute
```

按ENTER，5秒倒计时。无需tunnel、sender或START，因为这是固定数据集回放，不是DAgger流程。不会自动复位双臂，也不自动保存三图。若只有手部起点不符，ENTER后会执行上面的同接口手部准备；若反馈仍不匹配，将打印偏差最大的关节并停止，不绕过门禁。

检查文件可去掉--execute。日志在G2 `/home/agi/xza/DM0.5/logs/diagnostics/training_replay_unified34_时间.json` 和同名 `.console.log`；报告含source/布局/名称/最终目标和反馈/控制统计。

## 本轮验证与限制

本地replay/operations纯逻辑测试49 passed，Shell语法及files-only通过。含新增模拟执行测试：选unified-34 normal，948帧34D action完整保留，无Split hand segment，结束释放owner；files-only不导入GDK。代码逐分支核对单次SDK请求，参考已读取GDK2.6.3官方Robot章节。

新43入口尚未真机验收，不能把之前单34D模型推理真机结果说成本回放已验收。数据集环境/起点/瓶位与当前环境有差异时，固定回放不会自动适应。

部署核对：本轮具体文件已同步G2；在G2执行43入口不带--execute，输出948帧/source=actions_34d/source_hz=30/servo_hz=100/layout=unified-34/channel=normal，并明确no GDK，退出0。未远程初始化GDK或执行回放。

2026-09-09 17点启动阻塞修复：53项operations/replay测试通过，覆盖手部准备时双臂保持、源数据不变、反馈到位及反馈卡住拒绝回放。启动前拒绝时不再调用无输出实例的软件emergency_stop，避免误导性latched消息；实际发送后的异常仍停止。错误原因写入JSON。新准备流程真机未验收。
