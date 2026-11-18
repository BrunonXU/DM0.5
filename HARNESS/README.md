# F1 Harness 机器人任务编排与验证

第一次了解 Harness，先读[《F1 Harness 核心思路与设计原则》](docs/ARCHITECTURE.md)。用一次单块抓放看感知、技能、程序和模型怎样分工，以及怎样验证动作结果。

准备现场运行时，先从[当前状态](agent/STATUS.md)核对已部署版本、可用路线和待验事项，再按[运行说明](docs/DIRECT_ROLLOUT.md)打开 [F1infra 任务页](http://192.0.2.7:4242/#tasks)。

本仓库保存可共享的开发源码和文档。现场地址和设备标识使用示例值，现场图像、运行原件及冻结发布包在本地保存。

## 人类阅读

| 入口 | 阅读时机 |
|---|---|
| [Astra 几何工具与固定技能](docs/GEOMETRIC_AGENT.md) | 模型可生成什么动作、预览如何反馈、哪些仍需现场验证 |
| [完整真机运行：当前入口](docs/DIRECT_ROLLOUT.md) | F1infra页面操作、一次网页“开始”完成任务、停止与结果 |
| [故障分级与有界恢复](docs/FAULT_POLICY.md) | 区分必须停止、候选拒绝、等待新观测、零发布重规划与诊断警告 |
| [第一次运行](docs/FIRST_RUN.md) | 理解 RPent／PyRUA、结果与可选离线程序体验 |
| [现场装配与启动](docs/SITE_STARTUP.md) | 维护人员核对粗标定开发档案、配置身份、打包与排障附录 |
| [Harness Agentic 问答](docs/AGENTIC_HARNESS_QA_20261111.md) | 2026-11-11讨论存档：抓取验证、恢复分工、VLA/RLT接入与自进化；原稿文献和数值未复核 |
| [运行与验证](docs/RUNBOOK.md) | 直接运行隔离模拟闭环、协议检查与测试 |
| [完整单块离线回放](docs/OFFLINE_REPLAY.md) | 机器人关闭时检查完整程序、验证等待恢复和阶段耗时；不代替真机验收 |
| [感知失败现场留证](docs/SCENE_FAILURE_EVIDENCE.md) | 原始RGB-D、反馈、搜索状态如何保存到同一次运行，及缺失证据的边界 |
| [标定交接合同](docs/CALIBRATION_HANDOFF.md) | 与负责实测标定的 agent 对齐输入和准入 |
| [任务计时与布局参考](docs/visualizations/task-timer/index.html) | 11月9日仅任务页随F1infra V44部署：大号秒表按秒显示，保留原配色和字体；此链接保留历史原型，发布验收见[工作台状态](../F1-4242/docs/STATUS.md) |
| [已确认的大字与三路画面](docs/visualizations/task-density/index.html) | 11月6日：正式页已随F1infra V42部署；仓内原型使用相机占位图 |
| [正式任务页](http://192.0.2.7:4242/#tasks) · [已确认原型](docs/visualizations/infra-tasks/index.html) · [设计记录](docs/TASK_WORKSPACE_DESIGN.md) | 任务配置、日志与实时相机；执行服务的实际部署身份及可用状态见当前运行说明 |
| [RPent 原版界面参考](docs/visualizations/upstream-ui/index.html) | 对照上游的执行输出、相机与动作时间线；隔离示例展示 |
| [上游复用清单](docs/UPSTREAM_REUSE.md) | 核对已有项目与上游组件的职责、固定来源和适配范围 |
| [F1 当前任务](../tasks/F1_ComplexyToys/README.md) | 了解现有机器人任务；F1 运行事实以其 [STATUS](../tasks/F1_ComplexyToys/STATUS.md) 为准 |

## Agent 接续

协作和发布验收遵循 [AGENTS](AGENTS.md)。本轮要做什么看[待办](agent/TODO.md)，要还原某个决定或现场结果查[实验索引](agent/experiments/README.md)。追溯 V6 至 V23 的故障诊断与当时的接续安排，可查[历史交接](agent/HANDOFF.md)。

## 源码与配置

| 你要做什么 | 从哪里进入 |
|---|---|
| 改任务编排、感知或机器人侧接线 | 主要实现在 [src/f1_harness](src/f1_harness/)。[机器人原生入口](native/entry.py)装载对应服务，[感知环境](perception/pyproject.toml)单独管理依赖。 |
| 使用日常命令或检查服务启动 | `f1-harness` 的子命令集中在 [CLI](src/f1_harness/cli.py)，网页协调器由 [serve_workspace.py](scripts/serve_workspace.py)启动。可从[真实启动合同测试](tests/test_real_bootstrap_contract.py)复核入口行为。 |
| 核查发布或复现验收 | 先用[发布登记](releases/registry.json)定位对应版本的冻结清单与回执。[正式回放驱动](scripts/replay_release_validation.py)准备输入、运行并审计输出。[跨工程测试](scripts/check_native_integration.py)使用模拟传输复核 DAgger 控制链。 |
| 追查一次性实验、现场记录或经验 | 从[实验索引](agent/experiments/README.md)找到同一问题的复盘，沿引用定位 `artifacts/` 的脚本和产物、`runs/` 的运行原件。查询 `experience/` 中候选与晋升的含义见[经验程序说明](docs/EXPERIENCE_PROGRAMS.md)。 |

新增代码、命令或实验时，归属按[协作规范](AGENTS.md#代码与实验归属)确定。[规划配置示例](configs/planner.example.toml)用于了解配置字段；实际运行身份见[当前状态](agent/STATUS.md)。

上游 RPent、PyRUA-Lean、ASPIRE、SAM2 在 `third_party/` 下独立检出，提交身份由[源码锁](configs/upstreams.lock.json)记录。在 Projects 根运行 `python3 HARNESS/scripts/bootstrap_upstreams.py` 可核对源码。新工作区缺少检出时加 `--fetch`，只下载源码。凭据由本地认证组件读取，私密配置的处理遵循[协作规则](AGENTS.md#私密配置与源码身份)。
