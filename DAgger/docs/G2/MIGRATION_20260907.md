# 独立 DAgger 迁移证据

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

- 本地根目录：`/home/user/projects/DAgger`；G2根目录：`/home/user/xza/DAgger`。
- 28个实现/支持模块迁入独立`g2dagger`命名空间（另有package initializer），8个测试文件，33–40操作/审计脚本、Windows sender/tunnel、配置快照、相关历史文档。
- HIL/录制源码与测试不再留作G2Teleop的活动实现；迁移前副本在`data/archive/legacy_source/`。G2Teleop的33–40脚本只负责迁移提示和转发，12号冻结入口及其支持组件未改。
- 新入口PYTHONPATH无需G2Teleop。G2的IK依赖复制为DAgger/vendor_ik；本地离线运行不要求安装真机IK。
- G2客户端、官方GDK/模型、Home配置和last_hand_command仍是明确的共享硬件依赖；最后成功目标不能变成两个相互独立的权威记录。这里的“解耦”是代码包/入口/录制数据与冻结遥操分离，不是复制完整机器人SDK。
- 本地与G2 `data/raw/` 共3410个文件、506577658字节，逐文件大小/SHA256完全一致，证据`logs/validation/data_copy_verification.json`及两个inventory。包括Powered HIL和真实采集Shadow历史轮次，未重写原始日志路径/时间戳。
- G2原始旧位置暂留回退数据。新运行journal/payload、主遥测/反馈/console写入DAgger。历史离线证据已迁本地`data/archive/offline/`，历史md里的原路径按历史理解。
- 本轮验证：本地DAgger **47 passed**；G2独立DAgger **47 passed**和40 files-only预检通过；迁出后的G2Teleop回归 **103 passed / 6 skipped**。6项缺Ruckig/Pinocchio。本轮不是新的真机验收。
- 首次本地测试在沙箱内的回环socket被拒绝；获准运行本地回环测试后通过，无代码修正来绕开失败。
- 本轮仅迁移和验证，没有改变运动参数、录制默认模式，也没有修复/放宽此前训练审计。`training_ready=false`继续有效。

入口：[根目录START_HERE](START_HERE.md)。详细讨论：[RECORDING_AND_PROGRESS](RECORDING_AND_PROGRESS.md)。
