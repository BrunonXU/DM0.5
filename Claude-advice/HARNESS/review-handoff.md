# 交给 Codex 的开工指令（第一轮）

这份文档是用户粘贴给 Codex 的开工指令。第一轮只做两件事：量出现状基线并上传数据；完成三条不需要用户确认的改动，让流程有机会第一次走通（里程碑 M0）。做完就停下汇报，不继续往后做。

指令里反复强调三点，因为这是过去 40 多版没有成功的原因：完成的标准是真机结果；不许新增拒绝原因；数据和报告必须上传到 GitHub。背景见[改造计划总览](review-roadmap.md)，指标定义见[验收指标与执行细节](review-acceptance.md)。

```mermaid
flowchart LR
  A[读规则和计划] --> B[A-1 漏斗脚本]
  B --> C[A-2 基线报告<br/>上传 evidence]
  C --> D[B-1 B-2 B-3<br/>即 H-01 H-02 H-03]
  D --> E[软件门票]
  E --> F[请用户做 10 次真机试验]
  F --> G[漏斗报告<br/>上传 evidence]
  G --> H[停下汇报]
```

## 指令正文（整段复制）

```text
你接手 F1 Harness 的改造执行。分工：Claude 只写审阅和计划，你负责执行、测试、提交、上传证据并回写状态。

【仓库】
BrunonXU/DM0.5。计划文档在分支 cursor/claude-advice-harness-review-538f（PR #2）。若 PR #2 已合并，从最新 main 新建工作分支；否则从该分支新建。不要直接推送 main，不要自己合并 PR。

【开工前按顺序读完】
1. 根 AGENTS.md
2. tasks/F1_ComplexyToys/AGENTS.md 和 STATUS.md
3. HARNESS/AGENTS.md，重点是 [CLAUDE_ADVICE] 一节
4. Claude-advice/README.md
5. Claude-advice/HARNESS/review-roadmap.md（为什么改、里程碑）
6. Claude-advice/HARNESS/review-architecture.md（各模块负责什么）
7. Claude-advice/HARNESS/review-acceptance.md（怎样才算做完，必须逐条遵守）
8. Claude-advice/HARNESS/review.md 中 H-01、H-02、H-03 的规格
9. Claude-advice/HARNESS/evidence/README.md（证据怎么上传）

【完成的标准，先记住】
- 验收只认真机漏斗指标：走到第几阶段、成功几次、耗时、模型调用次数。测试通过、回放通过只是上真机的门票。
- 没有真机试验数据的条目，只能标“软件完成，待真机验收”，不得标 [x]。
- 每个 PR 报告“新增拒绝原因数”，除六类硬保护外必须为 0。检查失败时的默认处理是用最新测量重算、换候选或告警后继续，不是停下。
- 指标没达到就如实报告差距，不得调低阈值、缩小布局集合或更换统计口径。
- 数据和报告必须上传到 Claude-advice/HARNESS/evidence/，没有上传的结果视为没有做。

【本轮任务，按顺序做】
任务 1（A-1）：写漏斗统计脚本 HARNESS/scripts/run_funnel.py。
  - 按 review-acceptance.md 第二节的定义，从运行目录的 collected/owner/harness/trace.jsonl 和 planner/trace.jsonl 计算：最远到达阶段 S0–S9、各阶段耗时、首动时间、任务时间、模型调用次数和等待时间、非物理拒绝次数、撤权次数、最后一个失败原因。
  - 文档里标“待核实”的事件名（例如模型调用用哪个事件）先在源码里确认；对不上就停下问我，不要猜。
  - 失败原因码的“物理 / 非物理”分类表放在脚本旁的 JSON 文件里，分不清的先归非物理，并把这张表发给我确认。
  - 脚本只读，不进入任何运行路径。个别运行缺文件时跳过并记录，不中断。

任务 2（A-2）：量基线并上传。
  - 在 6000D 上对 HARNESS/runs/workspace/ 下全部历史运行跑 run_funnel.py。
  - 抽 5 条运行，人工对照原始记录核对脚本结果。
  - 写基线报告：各阶段到达率、最常见的 5 个失败原因及次数、非物理拒绝的均值、模型调用的中位数、首动时间中位数。
  - 按 evidence/README.md 上传到 Claude-advice/HARNESS/evidence/<今天日期>-A2-baseline/：report.md、funnel.json、最近 10 次运行的原始记录、manifest.json。上传前脱敏，不上传视频、RGB-D、凭据和 site 配置。
  - 提交后运行 git ls-files Claude-advice/HARNESS/evidence，把文件数写进报告。根 .gitignore 会忽略 runs 目录，注意用 git add -f 或加例外。
  - 做到这里先开一个 PR 并告诉我，再继续任务 3。

任务 3（B-1、B-2、B-3，即 H-01、H-02、H-03）：
  - H-01：复核时只比较必需相机，可选相机集合变化改为“重扫同一候选”。“头部流是否算必需相机”按“不算”实现，并在提交说明里写明。
  - H-02：观测准备时限 4 秒改为 15 秒。
  - H-03：只读验证的重查次数独立计数，上限 6。
  - 每条单独提交，提交信息以 "H-01:"、"H-02:"、"H-03:" 开头。只改规格列出的文件，不顺手重构。

任务 4：软件门票。
  - 全量 pytest、scripts/check_native_integration.py、一条正常流程和一条最近真实失败的零输出回放、scripts/check_project_layout.py。
  - H-01 另用 V40 现场原输入回放，确认首发前进入重扫而不是撤权。
  - 测试在 6000D 的项目 venv 里跑。

任务 5：请我做真机试验。
  - 门票全部通过后停下，告诉我可以试了。我会在 F1infra 4242 任务页，用立放布局做 10 次，每次点一次“开始任务”。你不发送开始指令，不部署到机器人之外的任何地方，不替我判断成功。
  - 我做完后，你对这 10 次运行跑 run_funnel.py，按 review-acceptance.md 第七节的模板写报告（含改动前的基线对比），上传到 Claude-advice/HARNESS/evidence/<日期>-B-first-run/。

【本轮的目标数字】
- 因相机身份撤权：0 次
- 因观测准备超时交回模型：≤ 1 次
- 因“验证不可用”终止：≤ 1 次
- 非物理拒绝：相对基线至少减半
- 首动（S1）到达率：10/10
- 至少 1 次无人工介入的完整成功（里程碑 M0）
达不到也如实报告：每次停在哪个阶段、最后的失败原因是什么。

【硬性约束】
- 六类硬保护一律不动：授权与会话身份；实时反馈新鲜度与失联撤权；限位、碰撞与实际路径扫掠；已发布、部分发布或发布状态未知时不重放；相机内参、标定与时钟域身份；原始记录写盘失败即停止。
- 不改 DAgger、F1-4242、RLinf 和 third_party/。
- review.md 中标 [!] 的条目本轮一律不做。
- 组件替换（cuRoboV2、6D 位姿跟踪）不做，用户已决定暂缓。
- 发布部署按 HARNESS/AGENTS.md 的流程，由我决定何时部署。

【回写与汇报】
- 条目完成后按 Claude-advice/README.md 的格式回写状态和执行记录，并带上证据目录路径。只有软件门票、没有真机数据的，写“软件完成，待真机验收”。
- PR 描述用中文。最后汇报：每条的提交号、测试结果（通过、失败、跳过数）、基线数字、真机 10 次的漏斗表、新增拒绝原因数、证据目录路径、与规格不一致的地方。
- 做完本轮就停，不要继续做后面的条目。
```

## 之后几轮的顺序

第一轮通过后，按[验收文档第六节](review-acceptance.md#六里程碑与模块的对应)的里程碑推进：第二轮做判抓和闭环对准（需要用户先确认 H-15、H-07 的待定项），第三轮做程序优先模式（H-16）。每一轮都沿用上面的“完成标准”和“回写与汇报”两段。
