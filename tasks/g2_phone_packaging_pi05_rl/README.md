# G2 手机装盒 Demo

## 目标

使用 G2 双臂普通夹爪完成：

1. 抓取 dummy phone；
2. 双臂换手；
3. 将手机放入打开的手机盒；
4. 抓取并对准盒盖；
5. 闭合手机盒。

当前学习路线为 `π0.5 SFT → 固定真机评测 → RLinf SAC-Flow + RLPD（精细阶段）`。
第一版环境 action/state 为 16D 双臂关节与夹爪表示，π0.5 内部保持 32D model action。

## 仓库边界

- `../../../PI0.5/`：上游 Physical Intelligence OpenPI 源码。
- `../../../RLinf/`：上游 RLinf 源码。
- `../../../g2_robot/`：现有 G2 机器人运行时，目前保持不变。
- 当前目录：只保存本任务的集成、配置、实验、评测和轻量编排代码。

不要把上游源码树复制进本任务目录。对 G2、OpenPI 或 RLinf 的修改应保持隔离并可单独审查。

## 目录

- `configs/`：任务、模型、训练、部署和奖励配置。
- `scripts/`：任务级数据、训练、评测和 Demo 入口。
- `experiments/`：实验定义和运行记录。
- `eval/`：成功判据、评测协议和评分工具。
- `docs/`：任务范围、物理 setup 和设计决策。
- `artifacts/`：小型摘要和 manifest；大型数据集与 checkpoint 不进入 Git。

## 里程碑

1. 用 10–20 条 pilot episode 验证相机、状态、动作和时间同步。
2. 冻结物理 setup、数据 schema、train/validation 划分与真机评测场景。
3. 采集约 80–120 条有效 human demonstration。
4. SFT π0.5 并进行固定场景闭环真机评测。
5. 根据分阶段失败率选择 targeted SFT，或用 SAC-Flow + RLPD 优化一个精细阶段。

## 当前状态

数采设计已形成，当前只允许 pilot 采集。正式采集前仍需验证 16D 动作映射、夹爪标定、
相机同步、物理支撑方案和 LeRobot/OpenPI 转换回读。
