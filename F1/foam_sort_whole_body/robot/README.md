# F1 泡沫分拣机器人端

本目录用于在6000D开发F1侧的示教回放、模型请求客户端和21D观测/19D动作接口适配。
原21D/19D是数据来源与关节回放合同；正式策略为state19/action23，详见[训练recipe](../docs/training/dm05_eef_delta_recipe.md)。
`f1_replay/` 已实现可换数据包的19D示教回放，入口为 `../scripts/replay.sh`，默认仅检查。
`f1_policy_client/` 提供在线state19/三图观测、HTTP请求、H20前缀检查与同步执行，
复用现有发布、调度与停止；入口`../scripts/inference_client.sh`，默认inspect。
模型及冻结EEF→IK在6000D，F1只需现有ROS和Python标准库。见[推理手册](../docs/inference_runbook.md)。
操作与数据替换见[回放说明](../docs/replay_runbook.md)；真机参数和所选数据仍须分别核定。
源码随发布清单定向部署到F1任务目录，使用核实过的F1 ROS/SDK环境运行。
训练和数据清洗继续使用任务的`scripts/`及`models/DM0.5/`，模型通用源码继续引用工作区DM0.5。

开始修改前阅读[机器人工作约定](../../AGENTS.md)和[动作接口合同](../docs/action_interface_contract.md)。
实现进度见[当前方案](../docs/training_plan.md)，
发布记录模板见[release_manifest.example.yaml](../deploy/release_manifest.example.yaml)。

## FK/IK数学接口

Demo固定使用`f1_ikfk_demo_v1`，参数、接口、源码快照与已知缺口见[IK/FK demo recipe](../docs/training/ik_fk_demo_recipe.md)。

[`kinematics.py`](kinematics.py)提供`ArmModel.fk()`、`ContinuousIK.solve()`与`solve_eef_prefix()`，依赖NumPy/SciPy，不导入ROS、不访问网络、不发送动作。
关节使用度、位姿平移使用m；配置路径显式传入，校验已验证配置SHA。`solve()`接收目标EEF及当前/上一接受关节解；返回`success`、位姿误差、限位与耗时，失败时`joints=None`。

`solve_eef_prefix()`接收**反归一化后的23D物理动作**（不接受模型归一化32D输出）、观测锚点arm14和求解参考arm14，默认只解前20步；锚点整段固定。成功返回双臂关节轨迹和原夹爪/底盘值，失败不返回部分可下发轨迹。新客户端补齐观测新鲜度、首步差、调度与整段检查；碰撞和实机动态仍需现场验收。

算法依据、配置来源、完整复现命令和精度/时延边界见[受限IK验证报告](../results/continuous_ik_20260916/README.md)。同一环境重复结果可复现，不保证EEF单独恢复原示教冗余关节，也不保证换库/平台逐bit相同。发布前按F1实际依赖重新验证，不能将6000D虚拟环境跨架构复制过去。
