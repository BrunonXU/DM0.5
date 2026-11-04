# HARNESS：机器人任务编排与验证

HARNESS 用于把模型的理解与规划接到可验证的机器人接口上，逐步建立从现场观察、任务判断到执行反馈的闭环。现有 F1 控制、推理与学习能力按职责复用，项目边界和实施顺序见[实施方案](docs/IMPLEMENTATION_PLAN.md)。

人类说明集中在 `docs/`；供 agent 接续工作的状态、待办和实验记录集中在 `agent/`。本页只提供导航，当前完成度、验收边界和下一步只在[STATUS](agent/STATUS.md)维护，不把方案描述当作已实现能力。

## 人类阅读

| 入口 | 阅读时机 |
|---|---|
| [实施方案](docs/IMPLEMENTATION_PLAN.md) | 了解目标、系统边界、实施阶段和验收条件 |
| [Why&How 在线方案](http://192.0.2.1:4243/?doc=harness-implementation) | 在固定阅读站审阅本地原件，包含完整积木例子 |
| [上游复用清单](docs/UPSTREAM_REUSE.md) | 核对已有项目与上游组件的职责、固定来源和适配范围 |
| [F1 当前任务](../tasks/F1_ComplexyToys/README.md) | 了解现有机器人任务；F1 运行事实以其 [STATUS](../tasks/F1_ComplexyToys/STATUS.md) 为准 |

## Agent 接续

先读 [AGENTS.md](AGENTS.md) 和 [STATUS](agent/STATUS.md)，再按本轮问题读取[待办](agent/TODO.md)、[实验索引](agent/experiments/README.md)与人类方案。逐次运行证据与私密配置不属于人类阅读目录。

## 源码与配置

上游源码分别在 `third_party/RPent`、`third_party/PyRUA-Lean`、`third_party/ASPIRE`，保持独立 Git，根仓库通过[源码锁](configs/upstreams.lock.json)记录身份。核对源码可在 Projects 根运行 `python3 HARNESS/scripts/bootstrap_upstreams.py`；新工作区缺少源码时加 `--fetch`，只下载源码，不安装或执行上游。

[规划配置示例](configs/planner.example.toml)记录官方 OpenAI、Astra Medium 与默认离线模式，尚无执行 launcher。本地 `API.MD` 保持私密，不进入 Git、网页、模型上下文或执行 worker。
