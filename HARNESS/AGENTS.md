# HARNESS 协作入口

这页是在 HARNESS 开工前必读的最小规则集：先读什么、按什么顺序做、哪些东西不能动、做完写到哪里。细则已移到专门的文档，本页只保留摘要和入口。

理解系统设计读[《F1 Harness 核心思路与设计原则》](docs/ARCHITECTURE.md)；接续现场工作先核对[当前状态](agent/STATUS.md)和[待办](agent/TODO.md)；Codex 执行改进项读 [Claude 审阅与执行规格](../Claude-advice/HARNESS/review.md)。工作区通用约定见 [Projects 根规则](../AGENTS.md)和[项目规则与协作习惯](../PROJECT_RULES_AND_PREFERENCES.md)。

2026-11-26：V54 已部署，前一版本是 V53。H-20 配对检查三路 10/10，本批沿用默认环境（production-gateway）；按判定规则胜出的是 fastdds-udp，随下一个版本钉死。V54 中期批 3 次试验分别到 S4、S0、S5，暂停，尚无真机成功，下一步按第四轮审阅依次推进 H-33、H-32。现场数字以状态页和待办为准。

```mermaid
flowchart LR
  A[读本页] --> B[读 Claude 审阅与验收指标]
  B --> C[核对 STATUS / TODO]
  C --> D[改代码：只动条目范围]
  D --> E[软件门票]
  E --> F[真机验收 + 证据上云]
  F --> G[回写状态]
```

## 每轮接续

0. Codex 先读 `Claude-advice/HARNESS/handoff/` 里最新的 Claude 交接（入口见 [Claude-advice](../Claude-advice/README.md)），按其中的指令执行；条目细节查对应的 review 文档。
1. 核对 STATUS 页首的当前目标、已确认决定、实际完成范围、未验收项和下一步，现场操作前复查实时状态。以最新用户指令与对应证据接续工作。
2. 设计改动按上方章节核对依据，依赖选型与源码来源查[上游复用清单](docs/UPSTREAM_REUSE.md)。按本轮问题选读实验与代码。
3. 涉及 F1 积木时，先读其 [AGENTS](../tasks/F1_ComplexyToys/AGENTS.md)与 [STATUS](../tasks/F1_ComplexyToys/STATUS.md)。修改 DAgger、RLinf、F1-4242 或其他工程前，再读对应局部规则；HARNESS 不另维护 F1 当前状态。
4. 结束前核对对应正文、STATUS、TODO 与证据链接，只更新发生变化的内容。未完成时留下已做、未验收、阻塞与可继续执行的下一步。
5. 并行工作只修改约定文件，合并前重读最新内容；保留他人的未提交改动，不覆盖历史证据。提交、推送、PR 与合并按 [Claude-advice 的 Git 与 PR 规则](../Claude-advice/README.md#四git-与-pr)执行：Codex 只在非 `main` 分支提交并开 PR，只有 Claude 审阅后合并到 `main`。
6. 阶段结束时按 [Claude-advice 的交接模板](../Claude-advice/README.md#三交接模板)写 `Claude-advice/HARNESS/handoff/YYYYMMDD-NN-codex.md`，第一节写需要人类做什么，第二节一次列全需要 Claude 裁定的问题。何时停下见[该页第一节](../Claude-advice/README.md#一codex什么时候停什么时候不停)；其余时间按 Claude 最新交接连续执行。

## [HARNESS_MAINLINE] 用户确认的方案主线（2026-11-11）

常规程序、策略判断和学习策略之间的分工见[职责与交权](docs/ARCHITECTURE.md#职责与交权)。单块任务的接续过程见[单块分拣示例](docs/ARCHITECTURE.md#单块分拣示例)。

### [HARNESS_FOUNDATIONS] 最重要的核心路线：先把原子能力做扎实（2026-11-11用户确认）

技能职责、抓姿与运输的衔接见[原子技能与控制](docs/ARCHITECTURE.md#原子技能与控制)。经验的形成与复用见[程序与经验](docs/ARCHITECTURE.md#程序与经验)。

### [PROGRESS_OBSERVE_REPLAN] 推进、重观测与重规划（2026-11-13用户确认）

设计依据见[观测与重规划](docs/ARCHITECTURE.md#观测与重规划)。实现和审查使用项目内的[推进、重观测与重规划开发方法](skills/progress-observe-replan/SKILL.md)。

### [CLAUDE_ADVICE] 审阅建议的执行（2026-11-24用户确认）

[Claude-advice/HARNESS/review.md](../Claude-advice/HARNESS/review.md) 是 Claude 对 HARNESS 的只读审阅和改进执行规格：前半部分给人看，后半部分给 Codex 逐条执行。分工原则、何时停下交接、交接模板、Git 与 PR、状态标记和回写格式，统一以 [Claude-advice 说明](../Claude-advice/README.md) 为准，本节不重复。下面只写 HARNESS 专属规则。

1. **开工前必读。** Codex 每次在 HARNESS 开工，先读该 review 的状态，再读本页和 STATUS。
2. **本轮做什么。** 用户没有另行指定本轮任务时，按 review 的优先级处理条目。用户当轮明确指定了别的任务时，先做用户的任务，review 条目顺延，不得夹带执行。
3. **冲突时以谁为准。** 依次为：用户当轮明确指令 > 本页及 [故障分级](docs/FAULT_POLICY.md) 中的硬保护和发布验收规则 > review 条目 > 其他文档的旧描述。review 与本页冲突时，按本页执行，并把该条目改为 `[!]`、写明冲突点，在阶段交接里一并提交 Claude 裁定。
4. **执行范围。** 只改条目列出的文件和行为，不顺手重构、不扩大范围。不改 DAgger、F1-4242、RLinf 和 `third_party/` 上游源码；需要改这些工程的条目另开任务，并先读对应的局部规则。
5. **硬保护不得移除。** 本清单是 review 执行时的权威，review 只引用它的代码位置。以下保护无论 review 怎么写都保持不变：
   - 授权与会话身份；
   - 实时反馈新鲜度与失联撤权；
   - 原生关节限位、自碰撞、环境碰撞，以及对实际路径的扫掠；
   - 已发布、部分发布或发布状态未知时不重放，零发布须由 owner 证明；
   - 相机内参、标定和时钟域身份；
   - 原始记录写盘失败即停止。

   放宽其他检查时，必须在提交说明中写明它对应的物理失效、适用阶段、故障等级和恢复方式，与上文“实现与执行边界”的要求一致。
6. **完成后的 HARNESS 额外回写。** 按 README 回写 review 状态之外，涉及部署或真机结论时，还要按本页规则更新 STATUS 和 TODO。
7. **完成的标准是真机指标。** 条目是否完成，以 [验收指标与执行细节](../Claude-advice/HARNESS/review-acceptance.md) 的真机漏斗指标为准：走到第几阶段、成功几次、耗时、模型调用次数。测试通过和回放通过只是上真机的门票；没有真机试验数据的条目只能标“软件完成，待真机验收”。每个 PR 按该文档的模板附报告，并报告新增拒绝原因数；除六类硬保护外，净增必须为 0。指标未达到时如实报告，不得自行调低阈值或更换口径。基线数据和每批真机试验的报告、运行记录必须按该文档“证据上云”一节上传到 `Claude-advice/HARNESS/evidence/`，随 PR 进入 GitHub；没有上传的结果视为没有做。
8. **验收不降级。** review 中的验收标准是最低要求；发布前仍须完成本页“每次发布前的全流程零输出验收”。

## 实现与执行边界

完整条文见[归属细则的“实现与执行边界”](docs/CONTRIBUTING.md#实现与执行边界完整条文)，故障处理见[故障分级与有界恢复](docs/FAULT_POLICY.md)。要点：

- 优先复用已固定身份的上游源码和现有 F1 能力，不另造一套通用框架。
- 真实运动只由控制所有者授权和发布；用户在 infra 任务页点击开始和停止。读取、预热、刷新都不授权运动。
- 模型输出只是建议，不能当作硬件状态、成功判据或已执行动作。
- 六类硬保护不得移除（清单见上方 `[CLAUDE_ADVICE]` 第 5 条）。除此之外，测试阈值、性能诊断和派生产物问题不得成为动作的一票否决项。
- 新增任何运行门槛，必须写明对应的物理失效、适用阶段、故障等级、证据和恢复方式。
- 修改网页 owner、授权或故障恢复时，另外运行 `scripts/check_native_integration.py`。
- 每次训练都接入 W&B；HARNESS 的推进不隐含切换模型、启动训练或替换生产控制。

## 代码与实验归属

改动前先找到正式入口、现有实现和调用它的测试。各类交付物放在哪里，见[归属细则](docs/CONTRIBUTING.md#代码与实验归属)。要点：可复用行为进 `src/f1_harness/`，命令进 CLI 或 `scripts/`，回归样本进 `tests/`，一次性实验进 `agent/experiments/` 和对应的 `artifacts/`，现场原件留在 `runs/`，验收证据的上传副本进 `../Claude-advice/HARNESS/evidence/`。

## 每次发布前的全流程零输出验收（2026-11-10 用户明确要求）

发布或部署新版本前，必须用保存的真实运行数据，沿正式入口走完整流程，最终动作发布器换成只记录、不向硬件输出的接收端。规则原文见[发布验收](docs/RELEASE_VALIDATION.md)。这是发布检查，不是真机任务中的中断条件，也不等于条目验收。

## 私密配置与源码身份

- 根 `API.MD` 是已有本地私密文件。凭据只能由已授权调用所需的本地认证组件读取；正文与取值不得进入终端输出、日志、模型上下文、网页、导出包或 Git。文档仅记录配置来源和可用性，不记录秘密值。
- 认证字段与业务输入分开，必要诊断只输出脱敏结果；不得为检索、摘要或说明将凭据文件发送给模型。
- 上游独立检出保持各自 Git 身份，忽略的源码目录由锁定清单索引来源、提交和适配关系。不要把嵌套 Git 目录误加入根仓库，也不关闭稀疏检出或改现有上游远端。
- 保留源码、配置、数据与运行身份的对应关系。对人的框架版本按“产品／技术＋V整数”独立登记，先核对发布登记与已预留编号；未分配时写未分配，不猜测 V1，也不套用 F1 控制版本号。

## 文档与证据职责

| 内容 | 唯一维护位置 |
|---|---|
| 人类导航 | README |
| 核心思路、各层职责、设计取舍与成功证据 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 协作、现场授权与发布验收步骤 | 本页 |
| 上游来源、复用与适配边界 | docs/UPSTREAM_REUSE.md；具体源码身份以锁定清单为准 |
| 运行故障分级、停止边界与有界恢复规则 | docs/FAULT_POLICY.md |
| 当前部署、已验证能力、现场缺口和证据 | [agent/STATUS.md](agent/STATUS.md) |
| 下一步动作、触发条件和完成依据 | [agent/TODO.md](agent/TODO.md) |
| Claude 审阅建议、Codex 执行规格及执行状态 | [Claude-advice/HARNESS/review.md](../Claude-advice/HARNESS/review.md) |
| 日常长页归并后的原文 | `agent/history/` 保存注明日期和替代入口的快照。已被引用的早期交接与实施方案保留其历史入口，版本复盘继续承接对应问题的后续追证 |
| 同一问题的实验结论与完整复盘 | agent/experiments/YYYYMMDD-question.md |
| 原始日志、逐次输出、测试回执与机器清单 | 所属 artifacts 或实际 run 目录 |

| 代码、实验与文档的归属细则，实现与执行边界完整条文 | [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) |
| 发布前全流程零输出验收的规则原文 | [docs/RELEASE_VALIDATION.md](docs/RELEASE_VALIDATION.md) |

文档维护规则（每个结论只有一份正文、STATUS 怎么更新、历史怎么归档、收尾运行 `python3 scripts/check_project_layout.py`）见[归属细则](docs/CONTRIBUTING.md#文档维护规则)。使用准确文件名 `AGENTS.md`，不另建第二份 Current 或 Latest 正文。
