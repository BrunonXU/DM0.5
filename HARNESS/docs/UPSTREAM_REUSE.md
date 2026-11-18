<!-- doc_type: design; status: implemented-with-limits; updated: 2026-11-18 -->

# HARNESS：上游复用与适配边界

**主运行链采用 RPent＋PyRUA-Lean，ASPIRE 提供经验整理与独立验证的方法。** RPent 承担工具和状态契约，PyRUA 让规划模型通过 Python 组合这些工具；F1 的执行仍交给现有控制与推理服务，HARNESS 只补任务适配。

**现已实际复用 RPent 的 Toolkit／RPC／MemoryManager，以及 PyRUA 的机器人对象、持久 Python cell、API 文档、ledger 和按需图片机制。** F1坐标、夹爪、原生IK和发布owner是本体适配。当前接通完整物体相对抓姿、接近向量、TCP 中间路点和只读规划预览，避免只开放 `skill(name)` 或有限候选菜单；当前部署与真实验证边界见[STATUS](../agent/STATUS.md)。

**11月17日按固定源码重新核对：本次正常抓取失败主要来自F1适配的对象身份、观测合同、恢复证据和后台进程生命周期。** 上游提供工具、程序和记忆基础，没有现成F1视觉／控制实现，也不自动把每次rollout变成已验证程序。V28未激活；V29继续沿这些复用点修复，并验明缺少完整抓姿时的局部观察能力，验收与部署状态以[STATUS](../agent/STATUS.md)为准。

PyRUA 上游的 LIBERO `move_pose`、RobotWin `move_to` 已体现“模型组合通用几何工具”的方式。本体适配需要坐标链、工具与夹持物几何、IK 和反馈，并非每个任务都必须训练新策略。F1 工具合同见[几何 Agent 说明](GEOMETRIC_AGENT.md)；不声称直接复用了上游仿真机器人的运动实现。

完整运行链路与各层的决定权见[核心设计](ARCHITECTURE.md#职责与交权)。

## 1. 固定来源与选择依据

### 2026-11-17：正常空抓后的源码对照

本次继续使用下表固定提交，未更换依赖。复核实际源文件后，修复落在现有F1适配和经验存储入口，不新增任务分类器、通用状态机或另一套控制栈。

| 上游实际能力 | 本次发现的适配缺口 | V29累计修复与验收范围 |
|---|---|---|
| PyRUA `RobotBase.task`读取`_current_state().task`，子类负责状态来源 | F1把状态读取绑定完整观测；仅打印任务说明便等待视觉并抛异常，同一cell后面的必要观察未执行 | F1重载静态任务读取；普通缺目标观察返回结构化未知，权限／传输等真实异常仍单列 |
| RPent Toolkit／RPC封装工具结果；PyRUA持久cell可在同一程序判断结果 | 默认宏先做整套预览，再让owner重复规划；未来运输建议在首动前同步计算 | 默认直接提交给原owner规划与复核；显式预览仍只读。未来运输数值前瞻默认不进入同步关键路径 |
| PyRUA ledger关联工具调用，执行器负责报告真实结果 | 缺证帧清除先前抓空事实；零发布和不可重试混淆，模型接到相互矛盾的恢复信息 | 历史证据与当前有效性分开；按实际发布量报告`no_motion`，零发布规划不占物理重抓次数 |
| RPent MemoryManager保存／合并语料；PyRUA默认host可使用本轮空只读语料 | 失败已落盘但未供下一次请求读取；旧版常驻exporter继续处理新运行 | 任务作用域内检索失败事实；真实cell独立归档。导出作业绑定源码身份，队列清空后退出；旧评审仅补充派生件 |
| SAM视频预测器提供mask传播；F1仍须证明目标身份 | SAM3无语义候选后无条件SAM2回退，能把手部当积木；遮挡前缩框还使重识别失败 | 语义为空时保持当前未知，不以任意非空mask替代目标；按已有语义种子来源有界重识，跨相机核对物理对象关联 |
| PyRUA的通用运动接口允许程序选择观察与行动顺序 | F1观测、预览、提交和复核均索取完整抓姿，局部深度被一并丢弃；另一相机的未见目标覆盖真实几何诊断 | 沿同一owner增加局部环境观测合同；未知pose保持未知，世界系观察运动消费当前工具与环境证据。完整抓取和孔相对动作仍索取各自需要的测量 |
| 几何处理属于机器人适配，不由上游框架提供精度保证 | 真实有效顶面只因占整mask的比例低而被拒；比例没有说明几何是否足够 | 保留绝对支持、尺寸、残差、竞争面和注册检查；占比低只作质量提示。实际失败帧与真实窄面反例分别回放 |

真实失败日志回归保存了3个Astra代码cell，并在正式任务入口将相关失败事实送入下一次模型请求。它们仍是探索记录：本条没有成功程序候选或晋升。自包含候选、独立运行验证和RPent晋升继续沿既有实现，模拟验证不表示现场扶正已学会。验收原件见[经验回放](../artifacts/base-repair-20261117/experience-replay-result.json)及[源码审查](../artifacts/base-repair-20261117/experience-code-summary.json)。

此前验收的不足在于：静止图像与理想动作后反馈通过，没有覆盖真实接近中的换相机、错误身份传播以及旧后台进程继续消费新运行。现在把实际运动RGB序列、完整RGB-D关键帧、当前场景缺测、原生平滑输出和发布后的进程身份分别绑定；不能把其中任何一项单独通过当成真机稳定或自主学习完成。

以下来源表和接口说明保留原核查范围；其中历史“待实现”不能覆盖当前STATUS。

| 上游 | 本方案固定提交 | 用途与选择依据 |
|---|---|---|
| [RLinf/RPent 源码](https://github.com/RLinf/RPent/tree/886b3b274d3dd30bbc15615ea512d65ae90bc8b3) | [`886b3b274d3dd30bbc15615ea512d65ae90bc8b3`](https://github.com/RLinf/RPent/commit/886b3b274d3dd30bbc15615ea512d65ae90bc8b3) | PyRUA 官方快速开始明确使用的 RPent 提交；作为初始兼容基线 |
| [DAGroup-PKU/PyRUA-Lean 源码](https://github.com/DAGroup-PKU/PyRUA-Lean/tree/e8f01dca744421d70684adbbc1713fd4d92f8aca) | [`e8f01dca744421d70684adbbc1713fd4d92f8aca`](https://github.com/DAGroup-PKU/PyRUA-Lean/commit/e8f01dca744421d70684adbbc1713fd4d92f8aca) | 本轮核查的 main；包版本为 `0.2.0`，复用机器人对象、cells、MCP 与规划循环 |
| [NVlabs/ASPIRE 源码](https://github.com/NVlabs/ASPIRE/tree/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7) | [`fa00f0ef2bc1d91d594b9aa502c22133c9f538f7`](https://github.com/NVlabs/ASPIRE/commit/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7) | 本轮核查的 main；固定参考源码，首期不安装其运行环境 |

[PyRUA 快速开始](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/docs/getting-started/quickstart.md)给出了 RPent 的上述固定点，但没有证明本项目的 F1 适配、当前传递依赖和官方 API 已联合通过。首轮验收通过后才能把组合标为“已验证”。

本轮同时看到 RPent main 为 `0fa63c5a191df7665b03a1569d7a46d85df6b175`，仅登记为后续升级参考，不自动代替初始基线。升级必须更新锁定清单，并复验受影响的 API、取消语义和产物格式。

前三个源码位置分别为 `third_party/RPent`、`third_party/PyRUA-Lean`、`third_party/ASPIRE`。各自保留原始远端；HARNESS 的锁定清单记录来源和适配关系。后续必要补丁在 HARNESS 单独留存来源、用途与适用提交，不把修改后的代码冒充原始上游。主运行环境只装前两者及 F1 适配所需依赖。

## 2. 直接复用与最小新增

| 层 | 直接复用 | HARNESS 待实现部分 |
|---|---|---|
| RPent | `Toolkit` 注册与调用、状态封装、工具结果、取消信号、`MemoryManager` | `F1Toolkit`，把有限工具调用转交既有 F1 服务并核验回执 |
| PyRUA | `RobotBase`、`Backend`、`ArmToolkit`、MCP、规划循环、调用 ledger、自动 API 文档 | `F1Robot`、host、F1 知识说明，以及可审查的注册适配 |
| ASPIRE | 失败分类、经验提案、独立 verifier、验证后入库、冻结评测与证据关联方法 | 面向 F1 的经验字段、判据与门禁；不能直接执行其仿真脚本 |
| 现有 F1 | 已登记的模型、RTC、IK、控制所有权、真实 START、通信保护、录制和反馈 | 经原控制所有者承认的任务调用边界；具体接口仍待设计与验收 |

F1 当前合同从[任务推理主文档](../../tasks/F1_ComplexyToys/INFERENCE_RECIPE.md)与[任务状态](../../tasks/F1_ComplexyToys/STATUS.md)读取。HARNESS 不复制一套 VLA 推理、轨迹执行或训练框架，也不因安装 RPent 而启动它的 LIBERO、Franka 或其他机器人服务。

### RPent 的确切扩展接口

Harness VLA 论文给出组合原子技能与学习策略的设计依据。下面列出 F1 接入时使用的 RPent 接口。[Harness VLA 论文](https://arxiv.org/abs/2607.08448v5)

固定版 [`rpent/tools/toolkit.py`](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/rpent/tools/toolkit.py)提供：

```python
Toolkit(*, dashboard_events, state=None, memory)
Toolkit.add_tool(name, spec, handler)
Toolkit.execute_tool(name, input_dict) -> ToolResult
Toolkit.get_env_state(*, command, result, elapsed_s) -> dict
Toolkit.cancel_active_and_wait() -> None
Toolkit.solved() -> bool
Toolkit.close() -> None
```

工具 schema 使用 `name/description/input_schema`；只读 handler 用 `@readonly` 标记，其他工具执行后捕获状态。F1 适配须提供真实状态、调用身份与结束原因，工具完成不能仅由 HTTP 请求返回或模型自述判断。

[`RobotSpec`](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/rpent/robots/robot_spec.py)描述机器人名称、prompt、CLI 参数、运行配置和初始化钩子，也有 `is_real_robot` 与交互探索能力字段。[注册器](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/rpent/robots/base.py)通过 `robots.<name>` 调用 `get_robot_spec()`／`get_toolkit()`。这些是复用点，不能据此声称 F1 注册已经存在。

### PyRUA 的确切扩展接口

持久 Python cell 中可基于工具反馈写条件分支、局部重试和按需观测。作者在运行原理中给出了程序示例。[PyRUA-Lean 论文](https://arxiv.org/abs/2610.01939v2) · [运行原理](https://dagroup-pku.github.io/PyRUA-Lean/docs/getting-started/how-it-works/)

[`Backend`](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/_backend.py)是机器人对象与执行者之间的小契约：

```python
execute_tool(name: str, input_dict: dict) -> Any
solved() -> bool
artifact(name: str, step: int = -1) -> bytes
env_steps() -> int | None
cancel() -> None
```

[`RobotBase(backend, *, ledger=None, primitives="full")`](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/_robot.py)负责公共状态、调用 ledger 和结束防护；子类定义相机、图像产物、状态工具和结果类型，实现 `_state_from_envelope()` 与 `_summary_dict()`，通过 `_readonly()`／`_stateful()` 调用工具。公开方法的签名和 docstring 会生成给模型的 API 文档。[官方扩展说明](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/docs/robots/adding-a-robot.md)要求不能支持的成员显式列入 `ABSENT_MEMBERS`；F1 若还没有可信在线成功判据，就不能伪造一个 `done` 值。

`RobotAdapter(name, robot_cls, host, knowledge, blurb, ...)` 连接机器人对象与宿主；host 提供 `Cell/add_cell_args/cell_from_args/boot_options/boot/dump_card`。现有 `RpentBackend` 已把 Toolkit 转为上述 Backend，`cancel()` 调用 `cancel_active_and_wait()`。参考[注册定义](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/robots.py)和[宿主公共适配](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/hosts/common.py)。

**当前没有第三方机器人 entry-point。** `ROBOTS` 是固定 tuple，`get_adapter()` 只接受名单内名称并导入 `pyrualean.<name>`；现有 CLI 不能直接使用 `--robot f1`。初始实现需要小型注册补丁或显式装配适配，保留补丁和固定点的对应关系；不以运行时随意改内部对象替代可追查的适配。官方新增机器人教程主要面向 XPolicyLab，README 中 AF8 真机集成仍标记 coming soon，不能作为现成 F1 驱动。

## 3. 官方 OpenAI 与固定 Astra Medium

已确认的产品选择是**官方 OpenAI API、`gpt-6-astra`、`medium`**。[规划配置](../configs/planner.example.toml)明确选择 `codex` runtime 的 Responses 路径，仍待实现参数映射并验收。下面同时登记上游两种 runtime 的协议差异，避免把参数名相似误当成请求相同：

| PyRUA runtime | 实际请求路径 | 固定 Medium 的方式 |
|---|---|---|
| `codex` | Codex CLI，通过自定义 provider 使用 Responses API | `--model gpt-6-astra --reasoning medium`；官方 API base URL 传给 `--base-url`，密钥仅由 `--api-key-env` 指向的环境变量提供 |
| `openai` | PyRUA 自带的 Chat Completions 循环 | `--model gpt-6-astra --openai-extra-body '{"reasoning_effort":"medium"}'`；该路径不能把 `--reasoning` 当作请求参数已经生效的证据 |

PyRUA 默认 reasoning 为 `xhigh`，需要显式覆盖；不因一次失败自动换模型或强度。上游参考 Codex CLI 为 `0.155.1`，其 Astra catalog 关闭 code mode 以保持直接工具调用。`provider_overrides()` 会规范化 `/v1` 并设置 `wire_api="responses"`，因此 API base URL 与完整 `/responses` 请求 URL 不能混填。证据见[Agent 配置](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/docs/guide/agents.md)、[provider_overrides](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/generate.py)与[play_episode](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/play.py)。

首次协议验收只用离线观测和无运动工具，核对实际 endpoint、模型、推理强度、图像输入、工具参数、错误处理及使用量记录。源码支持配置不等于账户可调用或请求已经通过。私密文件仅由授权认证组件读取，不写入 prompt、工具输出、经验库或网页；本页没有读取其内容。

## 4. 进程隔离、取消与物理执行

[PyRUA sandbox](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/sandbox.py)限制 imports、把 `open()` 约束在工作目录、屏蔽部分 introspection，并记录 `sandbox_flags`；源码也明确说明进程内 Python 无法完全封闭。审计标记中有些仅用于记录，并非全部阻止执行。[`cells`](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/arms.py)在宿主进程中直接 `exec`，不是每 cell 独立进程的硬隔离。

[`runner.py`](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/runner.py)的 watchdog 会请求结束 episode 并取消工具，但 RPent 固定版 `cancel_active_and_wait()` 是设置 `cancel_event` 后无超时等待 `done_event`。它依赖 primitive 在安全取消点检查信号，不能保证阻塞或失联服务已停止。ASPIRE 的[README](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/README.md)也明确指出 trial/watchdog 不是安全沙箱。

F1 适配因此至少需要以下可验收边界，当前均为待实现：

1. 每 episode 独立进程与外部期限监督；只开放明确的 F1 服务能力，不向生成代码暴露驱动、私密配置或生产目录。
2. 指令带会话、所有权、有效期与取消身份，继续服从实际控制所有者的状态机；旧请求重放、超时迟回复和重复提交不能继续动作。
3. 取消结果区分“已请求”“执行者已确认”“物理状态未确认”。父进程退出或 planner 超时不能冒充机器人已停；失联由原控制保护处理。
4. 真实运动须取得实际控制所有者下的新鲜本地真实 START 授权。HARNESS 不远程生成按键、恢复旧授权或改变原碰撞与反馈保护。

## 5. 记忆存储复用与经验晋升

RPent 的 [`MemoryManager`](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/rpent/memory/manager.py)可以直接复用本地语料管理：

```python
MemoryManager(root, *, memory_access="read_only", inbox_cell_tag=None)
validate() -> list[str]
rebuild_index() -> Path | None
merge_memory(*, cell_tag, run_state_dir, solved) -> dict
```

它提供 global/suite/task_only 分层、YAML metadata、evidence、confidence、合并锁和冲突留存。[内存文件工具](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/rpent/memory/tools.py)区分 published 只读与当前 inbox 可写，但对 memory namespace 之外的文件不提供完整文件系统隔离；仍需遵守前述进程和目录边界。

**PyRUA 尚未自动接通该跨回合记忆。** 其 [LIBERO host](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/hosts/rpent_libero.py)默认建立本次运行的空 `_memory`，prompt 使用静态 knowledge 或复制的 guides。初期可把已验证的 F1 经验快照交给现有 `--guides-dir` 注入，并保留内容 hash；后续再评估是否需要单独检索工具，不先实现通用向量数据库。

RPent 的 `merge_memory()` 对 global/suite 草稿完成 schema 校验后即可合并，`solved` 仅门控 task audit/recipe 的复制。因此 **`solved=True` 或 `confidence=verified` 字符串本身不是独立验证**；HARNESS 必须在调用 merge 前增加经验晋升门禁。

下表的提案、独立复现与判定取自 ASPIRE 的调试工作流。协调者和验证者分别有自己的输入与职责。[ASPIRE 论文](https://arxiv.org/abs/2607.00272v2) · [工作流](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/.claude/skills/iterative-debugging/SKILL.md) · [协调者说明](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/.claude/skills/iterative-debugging/main-agent-prompt.md) · [验证者说明](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/.claude/skills/iterative-debugging/verifier-prompt.md)

| 步骤 | F1 适配时保留的要求 |
|---|---|
| 提案 | 从实际失败与修复证据提取适用条件、工作片段、退出条件及反例；保留来源运行与版本 |
| 独立验证 | verifier 只得到经验说明和允许的输入，不读取提案者的完整解法；重新复现其声称的效果 |
| 判定 | `verified/refuted/void` 分开；只晋升 verified，运行无效不算反证也不算成功 |
| 入库 | 写入固定经验快照，记录 verifier 回执、内容 hash 与适用机器人／合同 |
| 后续检验 | 能传达原任务的修复不等于能泛化；跨任务效果必须增加新的独立证据 |

ASPIRE 的 Codex skill 只是指向上述 canonical 文件；其评测脚本调用 `cap-harness`，内置技能片段使用其机器人 API。首期只改造工作流与记录合同，不能直接把仿真种子、坐标、脚本或现有经验标题搬成 F1 已验证技能。开发样本、独立检查与最终评测保持分开，最终评测失败不回灌用于同轮调参。

## 6. 依赖、模型服务与许可证

### 环境与传递依赖

建议为 HARNESS 核心新建 Python 3.11 环境。固定 [RPent pyproject](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/pyproject.toml)要求 Python `>=3.10,<3.13`；[PyRUA pyproject](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/pyproject.toml)要求 `>=3.10`，基础 `dependencies=[]`。后者的 numpy、Pillow、mcp 1.x、uvicorn、httpx 等实际运行依赖需要由明确环境提供，不能将空依赖表理解为无依赖。

RPent 固定版包含 `pydantic-ai-slim>=2.1` 等开放范围，机器人 extras 又引用浮动 Git 分支。两个主仓库 SHA 只固定源码，未固定完整环境；后续首次解析必须记录所有传递版本和 Git 提交，再进行导入与接口验收。不同机器人 extras 的 Torch／Transformers 等约束冲突，上游也要求分环境。

官方 LIBERO-PRO 示例的 Torch `2.7.1`／torchvision `0.22.1`、CUDA、仿真器与权重不作为 F1 核心环境的默认安装项。ASPIRE 的[依赖配置](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/pyproject.toml)与[模型来源锁](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/configs/dependency-lock.json)保留在其自身边界；需要运行其基准时再单独准备环境。

### SAM3 同名服务不能默认互换

| 使用方 | 来源与已核查约束 | 本项目处理 |
|---|---|---|
| RPent | `RLinf/sam3`；固定 RPent 的 extra 指向该仓库 main | 若确需该服务，再补固定来源与协议验收 |
| PyRUA RoboDojo installer | `RLinf/sam3@c1f6b1e83967c912f675d8975d437ccdc41e17d8`；保留已有 torch，numpy `<2`、opencv-headless `<4.12` | 仅作为该机器人经过测量的来源，不自动安装到 F1 |
| ASPIRE | `Max-Fu/sam3@6fe87d64a5beb9084923d7a9e002741178635b09`；provider 使用 torch `2.9.1`／torchvision `0.24.1`，整体另有 numpy `1.26.4` 等覆盖 | 独立环境与服务；首期仅参考其经验方法 |

证据见 [PyRUA installer](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/integrations/xpolicylab/PyRUA_Lean/install.sh)、[ASPIRE submodules](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/.gitmodules)与上述依赖锁。PyRUA 有现成 [`Sam3Http`](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/hosts/sam3_http.py)分离模型进程，但它不是 ASPIRE 服务协议兼容的证明。复用感知服务前要核对图像、mask、相机身份、坐标语义和返回类型，不能只按端口或 `sam3` 包名判定。

### 许可证与第三方边界

三个主项目均声明 Apache-2.0；再分发派生文件时保留相应许可证、NOTICE 和修改归属。该声明不覆盖全部模型、子模块与数据。

| 材料 | 已核查的独立边界 |
|---|---|
| RPent 与 PyRUA 主源码 | Apache-2.0；PyRUA NOTICE 记录部分 MCP／knowledge／工具 schema 来源于 RPent |
| ASPIRE 主源码与经验流程 | Apache-2.0；第三方仍按各自许可 |
| ASPIRE Contact-GraspNet | dependency-lock 标注 NVIDIA Source Code License，限制为非商业研究与评估 |
| PyRUA RoboColiseum IK 描述 | NOTICE 标注所带 GenieSim 描述文件为 MPL-2.0；不是 F1 首期必需组件 |
| SAM3 权重 | gated 下载及独立使用条件；源码许可证不替代权重条件 |

来源：[RPent LICENSE](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/LICENSE)、[PyRUA NOTICE](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/NOTICE)、[ASPIRE LICENSE](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/LICENSE)、[ASPIRE NOTICE](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/NOTICE)及其依赖锁。上游使用的其他 fork 只登记来源与必要性；首期不批量检出它们，也不改写其远端。

## 7. 首轮待验收清单

| 门槛 | 最小证据 | 完成后才能声称 |
|---|---|---|
| 核心环境 | 固定传递依赖、模块导入、所用 API 与上游固定点一致 | RPent＋PyRUA 在本环境可装配 |
| F1 最小适配 | fake backend 下观测、拒绝、结束、重复调用与取消回执测试 | F1 适配合同在离线条件下工作 |
| 官方 API | Astra Medium 的实际请求身份、图像与无运动工具调用、错误与使用量记录 | 规划服务链路已接通 |
| 原控制端接口 | 只读身份校验、过期／取消／失联检查、零输出证据 | 具备进入下一阶段的接口基础 |
| 经验晋升 | 候选隔离、独立 verifier、拒绝未验证提案、快照 hash | 经验流程已接通，不代表真机泛化 |

上述门槛通过后，物理执行仍按[核心设计的验证分层](ARCHITECTURE.md#怎样验证)和 F1 现场合同单独验收。源码检出、文档完整或一个仿真示例成功，都不能替代真机授权、闭环效果与失败恢复证据。


## 本次实现映射

核心依赖已锁在 `uv.lock`，感知依赖单独锁在 `perception/uv.lock`；上游原件保持 clean。F1 注册通过 `scripts/build_overlay.py` 生成 overlay，知识文件与机器人适配留在本项目。SAM2 提交和权重摘要在对应 lock 文件中。

运行入口使用薄 Responses driver，固定 Astra medium，实际工具列表只有 Python cell 与 finish；因此不依赖关闭 Codex shell 的配置是否被某版 CLI 接受。PyRUA 的 ArmToolkit、MCP、API reference 和统计仍直接复用，worker 放入 bwrap；MCP 执行动作工具明确 `readOnlyHint=false`。RPent 继续提供 Toolkit、HTTP RPC 与 MemoryManager。

独立真实 API 协议和模型驱动 synthetic 恢复链路已运行；完整 HTTP→Unix socket→owner mailbox 链路也已通过模拟集成。SAM2.1 tiny 在真实保存 ROI 上完成 CPU 分割和三帧跟踪，未测 GPU 延迟。ASPIRE 在本期是验证流程参考，未宣称运行其仿真环境。具体回执与局限见[当前状态](../agent/STATUS.md)。

## 2026-11-10 对照上游后的接口修正

固定源码中，RPent LIBERO已有 `move_pose(xyz,target_pitch,target_yaw)`，RoboTwin `move_to(arm,xyz,quat)` 委托原生规划器，Franka提供TCP增量与VLA技能。PyRUA通过公开签名生成模型API，用Python计算参数、检查结果和组合程序。此前F1仅开放技能名称是本地适配限制，不是上游要求。

本轮补齐 `skill(name, pitch_offset_deg=None, variant=None)`、`strategies()`，共享参数化恢复helper和固定运输宏，恢复上游 `images="on-demand"`。原始上游检出未修改；图片分块传输、F1请求身份与机械执行仍由本地适配实现。通用任意6D位姿／路点接口未宣称实现，当前最小范围是单竖直方块的物体相对几何变化。

- [RPent LIBERO 姿态联动](../third_party/RPent/robots/libero/tools.py)
- [RPent RoboTwin 原生规划委托](../third_party/RPent/robots/robotwin/primitives.py)
- [PyRUA 程序组合示例](../third_party/PyRUA-Lean/docs/getting-started/how-it-works.md)
- [F1 当前API适配](../src/f1_harness/adapters.py)

跨运行经验仍需要独立验证；不能把PyRUA持久cell或RPent文本记忆直接称为已经吸收了现场成功经验。本轮真实参数经验晋升为零。

## 2026-11-11：Prompt与控制权交接复核

**以下是V1的审计基线。条件程序交互V2已修正API签名、结果类型与图片说明，新增当前决策摘要和统一宏停点，并在两次实际模型调用中首轮显式接续。** 完整参考规则仍保留；4次受限验证与部署边界见[费用及交互对照](../agent/experiments/20261111-api-cost.md#条件程序交互v2四次受限验证与部署)，不将上游基准当本机提速证据。

**当前V1的程序化执行能力已经具备，主要缺口是接口说明与实际返回不完全一致，以及初始定位程序在预览后提前返回模型。** 本节核对部署包 `d9d8b5efa52a` 的源码与首条真机记录，并查阅开源实际实现；只读研究，0新增付费调用、0机器人连接，未修改运行代码或部署。

**交互优化与请求等待须并行处理。** 真机 `web-8970e304449344299411afdcbf52f579` 点击后7.95秒发出首请求，模型请求37.95秒，其中响应头前37.787秒、提供方报告processing 6.472秒且连接复用。首个cell只定位和预览，52.70秒返回，52.90秒开始第二次模型请求，用户取消前0动作提交。时间口径不同，不能把差值全部归为代理或模型思考。见[原始审计](../artifacts/v1-first-rollout-20261111/summary.json)与[实际cell](../runs/workspace/web-8970e304449344299411afdcbf52f579/planner/task/cell_003.py)。

### 已证实的合同问题与尚未证明的影响

| 发现 | 源码与边界 | 建议 |
|---|---|---|
| 自动API说明混入LIBERO结果类型 | `F1Robot`未声明`RESULT_TYPES`，上游生成器回退到LIBERO dataclasses，并宣称字段用属性访问、给出0.30m平移异常示例；F1核心工具实际返回dict | 生成F1专属返回合同，完整说明成功、失败、已运动、检查点和缺证据语义；不复制仿真限制 |
| 自动函数签名丢失`**` | 上游`_format_parameters`未处理`VAR_KEYWORD`，实际请求出现`inspect_plan(name, parameters)`，真实接口为`inspect_plan(name, **parameters)` | 从真实签名正确生成文档，并覆盖可运行的关键调用例子 |
| 图片说明冲突 | worker用`images='on-demand'`，python工具description却称移动后返回当前图片；本地knowledge另有按需说明，薄driver未采用上游按配置生成的完整arm contract | 同一个来源生成执行合同与tool description，明确自动附图、主动索图及cell返回边界 |
| 缺几何分支的示范没有贯通执行 | task prompt要求一cell定位后inspect_plan；完整宏另在长篇恢复说明中。最新真实cell在可行预览后打印即结束 | 给出定位→预览→依据返回字段选择候选→显式调用既有执行宏的完整示例；预览仍不等于运动授权 |
| 顶层状态摘要落后于本次工具证据 | 最新真机返回的state仍为unobserved／unavailable／authorized=null，primitive_calls却已有实测场景和可行预览；摘要读取_last_state缓存 | 从已有可信回执投影当前阶段、未解决问题和继续入口，不为刷新摘要额外触发感知 |
| 宏停点字段未统一兑现 | prompt说program_stage标识停止技能，但place_held_block多条失败路径直接返回底层结果，无统一附加字段 | 所有宏分支统一返回已完成步骤、当前停点及owner检查点，区分已解决历史失败与当前阻塞 |

前三项为确定的说明缺陷，后两项为摘要/返回合同缺陷；尚未证实它们分别造成多少秒或多少次误调用。第四项的提前返回有真实轨迹支持，但不能据此承诺任意新画面均能一轮完成。最新反馈约4782个JSON字符，已有结构化ledger及诊断去重；不应称本条被海量日志拖慢。证据：[渲染后的真实F1 API](../artifacts/agent-harness-review-20261111/generated-f1-api.txt)、[只读审计与源码身份](../artifacts/agent-harness-review-20261111/local-audit.json)、[F1适配](../src/f1_harness/adapters.py)、[上游生成器](../third_party/PyRUA-Lean/src/pyrualean/prompt.py)、[任务提示](../src/f1_harness/task_runner.py)、[恢复说明](../src/f1_harness/astra_programs.py)、[宏实现](../src/f1_harness/recovery_programs.py)。

### 开源机制与F1取舍

| 来源 | 实际机制 | 对F1有用的部分 |
|---|---|---|
| [PyRUA play prompt](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/play.py)、[程序示例](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/docs/getting-started/how-it-works.md) | 持久namespace；执行合同、机器人API、knowledge、任务卡分层；数值反馈可在同cell继续，依赖新视觉判断才看图 | 对齐F1执行合同与少量完整反馈程序示例；现有宏和自动finish继续复用 |
| [smolagents CodeAgent prompt](https://github.com/huggingface/smolagents/blob/main/src/smolagents/prompts/code_agent.yaml) | 输出schema已知即可在一cell连续调用；格式未知才打印给模型判断 | 把preview/skill的真实返回结构说明清楚，让Astra能可靠写条件分支；不照搬任意错误重试 |
| [Pi AgentToolResult](https://github.com/earendil-works/pi/blob/main/packages/agent/src/types.ts)、[codemode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/codemode.md) | content给模型、details给日志/UI、structuredContent给程序；脚本主动输出才进入模型上下文 | Python保留完整结构化诊断，Astra默认获得决策相关事实和必要图像，原件保留可查。减少重复信息流，不删空格 |
| [LangGraph workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents)、[现存prebuilt循环](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py) | 条件边可走程序或结束；默认ReAct仍在工具后回模型，旧prebuilt结构化终答还可增加调用 | 借鉴明确的控制权交接；无需迁移框架，也不为仪式化规划/复核增加LLM调用 |
| [Code as Policies示例](https://code-as-policies.github.io/prompts/tabletop_ui.txt)、[项目](https://code-as-policies.github.io/) | 用少量命令→反馈程序示例教模型组合循环、条件及几何工具 | 示例教“如何依据当前观测决定”，不写死新积木的位置和抓姿；其网页也说明API查询造成长停顿，程序化不自动解决网络等待 |

RPent本身仍有工具→模型循环。固定版[LIBERO prompt](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/robots/libero/prompts/system.py)包含长篇经验与逐primitive看图要求，不宜整体移植。其[图像历史预算](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/rpent/planner/api_loop.py)有参考价值，但F1不应直接逐轮删改旧历史：先测重复图像和必要证据，避免损伤身份判断及已保留的上下文/缓存。

[PyRUA作者README](https://github.com/DAGroup-PKU/PyRUA-Lean)报告同模型、同动作工具的700个基准任务实例中成功率63.1%→71.7%；在两种方式都解出的实例上，调用数少49%、输入tokens少65%。这是作者基准，不是本项目实测，也不是墙钟耗时或F1成功率保证。

### 建议实施顺序与对照

1. **统一合同及示例。** 常驻prompt保留任务目的、真实API/返回类型、控制权交接、不可重复执行语义；正常完整程序优先。当前失败专属解释随结果提供，详细字段文档按需查阅。保留连续姿态/路点能力，不缩成预置抓姿目录。
2. **让已有程序真正接续。** 定位后的可计算预览、有限候选尝试及正常技能结果由同一程序消费；新视觉语义、现有候选均失败、程序外新策略需求才回Astra。已发生运动而结果未知时继续原请求的查询/恢复，不借新程序重放。
3. **分开程序数据、模型证据与全量日志。** 模型结果包含阶段、是否实际运动、缺失证据、失败原因、已试候选、下一项需要模型决定的问题；精确几何仍可供程序使用。决定必需证据不得因缩短文本被隐藏。
4. **同时查首请求长等待。** 保留同一request的写出、响应头、处理时间、连接复用与取消后用量回执；不以普通GET或单条processing-ms差值判断根因。首轮仍需38秒时，减少一轮也不能兑现20秒首动。
5. **按冻结任务质量验收。** 标准档medium不变；对照初始几何可用、缺实体、首候选失败、动作后证据未知、正常完成等情形，检查真实API程序可执行、无重复动作、独立摆放成功率，以及首动/整条任务P50/P95、模型轮数与费用。未知分支允许必要的新决策，不硬限两轮制造漏检查。

当前研究不宣称获得速度或精度改善。全流程稳定后再做独立验证的经验复用；持久cell、文本记忆及成功程序都不等于自动训练新形状能力。紧凑JSON/去空格按用户决定不接线。


## 原子运动候选依赖：OMPL（2026-11-11，未部署）

OMPL 生成关节空间候选路径，由 F1 使用原限位、模型及持物和环境碰撞检查来复核、执行与验证。分工见[核心设计的原子技能与控制](ARCHITECTURE.md#原子技能与控制)。

库的搜索方法见 OMPL 论文及源码。MoveIt 的规划接口用于对照目标约束、附着物碰撞和轨迹验证的职责。[OMPL 论文](https://kavrakilab.org/publications/sucan-moll2012open-motion-planning.pdf) · [OMPL 源码](https://github.com/ompl/ompl) · [MoveIt 规划接口](https://moveit.picknik.ai/main/doc/concepts/motion_planning.html)

候选固定OMPL 2.0.1，采用[官方RRTConnect实现](https://ompl.kavrakilab.org/classompl_1_1geometric_1_1RRTConnect.html)；[原论文](https://doi.org/10.1109/ROBOT.2000.844730)支持双向搜索方法，不提供F1可执行性保证。[官方状态／运动校验说明](https://ompl.kavrakilab.org/stateValidation.html)明确需要应用提供碰撞与运动检查，本项目复用原F1模型并在返回前细分验证，不接受近似解。

[依赖锁定](../configs/ompl-candidate-requirements.txt)包含CPython 3.11 x86_64开发与CPython 3.12 aarch64候选轮子的SHA256；[下载来源回执](../artifacts/atomic-motion-20261111/ompl-dependency.json)记录官方PyPI文件。当前仅解压至隔离开发overlay；ARM轮子仅下载，尚未安装／验收。不改全局环境，不改原RPent/PyRUA/ASPIRE的固定SHA。能力和限制见[实施复盘](../agent/experiments/20261110-sam-rollout.md#原子运动基本功统一碰撞边界与关节空间搜索2026-11-11未部署)。
