# F1 Harness 架构优化历史交接（截至 V23）

这份交接止于 2026-11-12 的 V23。追溯早期抓持证据丢失、运输拒绝或旧图与新帧错配时，可以按对应版本找到当时的判断、输入和验收回执。

今天接手项目请查[当前状态](STATUS.md)和[待办](TODO.md)。系统核心思路统一维护在[架构正文](../docs/ARCHITECTURE.md)，现场授权与发布验收见[协作规则](../AGENTS.md)。

## V23 交接快照

**2026-11-12：观测关联与零发布续跑 V23已部署，两条路线共用，infra V56保持。** 基于已完成的共享V22合入旧图初始化SAM跟踪：模型等待期间目标移位后，先在模型实际所见图上建立跟踪，再以当前RGB-D重建几何，避免旧框把桌面阴影当成积木。V22已包含本轮预算／回执保留及明确stuck提示，V23完整保留共享恢复、VLA及两条订阅入口；标准档＋medium不变。

真实失败图像对照、实际失败回执／耗时复现、冻结接口回归、正式全链零输出及四项输出审计、ARM只读启动与三端／双订阅池核验完成；**未发送START，真机完整抓放与脱手恢复仍待验。** 全链后续场景／机械反馈模拟；独立按源时间节奏的连续复查在用户催上线后未再运行。8.71°估计变化、进一步减少终态模型调用、moved语义，以及最新V22放置证据不足仍待接续。见[同一复盘V23节](experiments/20261110-sam-rollout.md#v23观测关联与续跑部署2026-11-12)。

## V23 之前的阶段记录

以下段落中的部署、待验项和接续安排均以各段标注的日期和版本为准。

**V22 当时与 infra V56 配套。** 两条模式统一了运行包及订阅配置，验收见[共享双路线复盘](experiments/20261111-vla-astra.md#v22共享harness双路线部署2026-11-12)。未由Codex发送START，下一步验证真实初始摆放泛化、外部抓取恢复及完整放置；旧提示跨帧错配的感知修复仍待独立验收上线。

V21曾短暂激活，但订阅预热入口核验发现打包覆盖错误，已由V22替换。V22现场服务与两条订阅池均核验通过；185／62／52项检查口径分别保留，V21外部抓取回放／ARM负载证据只复用于内容哈希未改变的组件，不伪称V22重跑。源码包含其他并行改动，后续仍从冻结release选取，不能整树覆盖部署。以下为历史交接。


**2026-11-12原子能力修复进行中，尚未部署本轮改动。** 原始RGB-D核对修正了此前第一条的归因：模型所见图中积木横躺，规划所用新帧中积木已立放并换位置；旧提示框在新图上分割到了桌面阴影，随后形成入桌约3cm的假物体。现有SAM在所见旧图初始化、再跟踪新帧的离线对照已找到真实积木；不能再把这条直接归为横躺物体配准误差。

观测预算／原回执保留、旧图初始化SAM接续和明确stuck终态提示已改源码并做针对性检查；正式全链、连续真实感知与发布仍待完成。准备发布时发现另一条共享Harness V21正在切换配置，不能覆盖其部署；本轮须接续其最终身份重新冻结。证据在 `artifacts/atomic-foundations-repair-20261112/`，两条失败与完整边界继续汇入同一复盘。


**2026-11-12最新现场接续：V20两条抓前零动作失败已查清，尚未修复。** 横躺几何包围盒与桌面冲突约3cm；立放计划成功后因8.71°姿态差复核拒绝，再因4秒观测预算包含5.35秒计算，立即失去本地重试，原诊断被覆盖。冻结源码离线复现与原图对照见[同一复盘](experiments/20261110-sam-rollout.md#v20两条现场横躺几何冲突与立放续跑预算缺陷2026-11-12)。模型订阅正常，不是429；尚未进入运输或脱手恢复，不能称原子抓取稳定。

本轮只读调查，未改生产源码、部署或发动作；先修明确预算／回执缺陷，并定位实际几何误差，保留碰撞／反馈保护。当时部署为 Astra V20／混合 V18。

**2026-11-12 交接记录：Astra V20官方ChatGPT订阅接入已部署，混合V18／infra V55保持。** 用户已确认API余额耗尽并完成Harness独立OAuth授权；仅Astra当前走订阅，GPT-6-Astra标准档＋medium不变。不要把本机Codex登录令牌当API key；不要读取、输出API.MD或订阅凭据正文。

真实订阅协议往返、实际429输入与正式全链零输出验收、三端与订阅池核验通过；真实运动仍由用户启动。用户明确先恢复接入并现场跑一遍，再讨论运动能力；罕见429不扩展为优先调度工程。当前事实见[STATUS](STATUS.md)及[同一复盘V20节](experiments/20261110-sam-rollout.md#v20chatgpt订阅备用接入与部署2026-11-12)。以下版本为历史快照。

**2026-11-12 交接记录：混合策略修复并部署V18，独立Astra V15／infra V55保持。** V17现场已复现：两次运动超时保持恢复后，`ik_waiting_for_fresh_policy`静止等待状态再次超时被`control_not_eligible`锁存；V18补齐此分支。当前新计划保留、旧chunk不复活，停止后迟到结果仍丢弃；RTC与50ms保护不变。

163项冻结／62项原生检查（重叠）、完整回放与审计、真实ARM相机／录制负载下内存输出、三端部署核验已完成，未发START。一次89.9ms静止间隔被保留HOLD，随后29个新计划接续。物理运动和放置仍待验，调度长尾及Astra429另行处理；证据见[STATUS](STATUS.md)和[同一混合复盘](experiments/20261111-vla-astra.md#v18静止等待新计划时序恢复2026-11-12)。以下旧版本交接保留历史。

**2026-11-12 交接记录：原子运输与关节搜索 V15 已部署，替代 Astra V10；混合策略独立保留 V13，infra V55。** 用户授权部署已完成三端核验，未发送START；下一步由用户在原infra选Astra验证。V14因ARM计算预算不足取消，V15保留原碰撞余量和细分精度，修正有界计算预算与离线驱动接线。

真实V10失败输入已通过正式PyRUA/RPC/owner运输接续及平滑输出审计；另一保存场景全链、规划中取消、ARM与只读boot通过。后续反馈／场景明确模拟，**该失败姿态后续精对准、真机完整抓放与真实脱手恢复仍未通过**。当前接续以[STATUS](STATUS.md)、[TODO](TODO.md)和[同一复盘部署节](experiments/20261110-sam-rollout.md#v15原子运输部署与arm补验2026-11-12)为准；下文11月11日快照保留历史，不能再作为实际部署状态。


**切换V15之前的交接快照（2026-11-12，北京时间）：当时Astra部署为持物连续性与请求接续 V10，混合策略已独立升级V13（输出恢复与停止确认，ARM零输出与部署核验通过），完整真机抓放闭环尚未通过。**

**第一轮源码修复和独立软件验收后，已按用户“部署上线吧”发布V7；三端核验通过，未发送START。下文V6诊断与部署快照作为历史保留，当前部署以STATUS及发布登记为准。** 当前接续以[STATUS](STATUS.md)和[同一复盘的实施节](experiments/20261110-sam-rollout.md#v6之后架构修复实施2026-11-11)为准：坏配对历史丢失与脱手停稳链已修，真实横躺几何已纠正；两条实际场景的可执行抓取／运输路径和现场恢复仍未解决。不要重新把已完成的软件修复列作未实施，也不要把模拟恢复当作真机成功。

**V10已接续修复V7首条现场的采样间隔误隔离、提交零发布拒绝与异常收尾，并部署三端核验通过，未发START。** 原测量held有效126→290份、重确认86→4次；正式零输出覆盖原上移提案和提交竞态，但上移后运输仍失败。后半程反馈模拟与真实测量重算分开登记，真实脱手恢复仍待现场。详见[同一复盘实施节](experiments/20261110-sam-rollout.md#v10持物连续性与请求接续实施和部署2026-11-11)；下文V6/V7诊断与方案保留历史，不代表仍未实施。

**V10最新现场转为明确路径问题：运输零发布，持物与清理修复已起效。** 四路线拒绝精确复现，其中variant2存在凸包误挡点：保留10mm余量时原网格仍+0.704mm，精细复核触发与Harness零阈值不一致。抓前已提示运输未解但宏仍抓取；Astra随后约27cm上抬在J6限位拒绝，corrections=1软提示下结束。离线修正该触发后下一点仍进入10mm保护包络，整条未通。当前未修、未再部署；先见[最新现场复盘](experiments/20261110-sam-rollout.md#v10首条现场运输零发布与路径规划缺口2026-11-11)，不要把单点误挡当作整条已可行。

混合支线交接记录（2026-11-12）：V13恢复／停止确认已完成ARM真实采集／录制／编码负载、原生B通信和三端核验并部署。两次卡顿均保持后新观测重规划，反馈与动作接收端明确模拟；真实运动与放置待验。旧停止未确认保留，新任务授权前仍须实测停稳；详见[STATUS](STATUS.md)与混合复盘V13部署节。上方V11和下方原子运动候选段为11月11日快照。

**原子运动基础候选已实施但未部署（2026-11-11）：** 当时将原子运动设计依据写入 AGENTS 的 `HARNESS_FOUNDATIONS`，结合问答与文献／开源依据。精细碰撞边界、抽稀自碰撞、可选OMPL关节搜索、右臂本体环境碰撞和新场景细分复核已实现；真实V10运输输入经数值RPC与F1Owner/Ruckig内存输出通过。实际部署仍Astra V10／混合V11。搜索默认关闭，尚缺多场景、完整正式PyRUA零输出、ARM与现场验收；接续看[同一复盘新增节](experiments/20261110-sam-rollout.md#原子运动基本功统一碰撞边界与关节空间搜索2026-11-11未部署)，不要把前面的“未修”诊断当成当前源码状态，也不要把理想跟随当现场恢复。

**V7首条现场新增反例：抓稳已成功，运输四候选拒绝；500ms间隔隔离造成预览held有效、提交又未知，接着零发布拒绝被误报为submission_unknown，清理异常覆盖终态。** 原持物历史未再永久丢失，但86次三帧重确认暴露质量门槛仍在阻塞；该缺陷现由V10接续修复；原始复现见[同一复盘](experiments/20261110-sam-rollout.md#v7首条现场抓稳后运输与提交接续失败2026-11-11)。混合策略支线与Astra主线版本分别以STATUS路由为准。

## 1. 交接引用的资料

当时的组件选型和现场来源见[上游复用清单](../docs/UPSTREAM_REUSE.md)、[同轮现场复盘](experiments/20261110-sam-rollout.md)与[执行和 SAM 的早期交接](../docs/archive/EXECUTION_SAM_HANDOFF.md)。

## 2. 当时的开发约定

该轮选用 Astra 标准档和 medium 推理强度，优先复用保存数据，单列真实模型调用目的、次数、用量与未知费用。早期“最多 5 次”是单轮额度。用户当时关注的具体问题是首次抓空、运输中脱手，以及感知短暂缺失把动作卡在原地。两条现场记录和当时提出的修复按问题保存在下文。

## 3. 部署与源码身份快照

| 项目 | 交接时值 |
|---|---|
| 当时产品 | F1 Harness 持物感知与连续执行 V6 |
| Bundle | `7d135c2748b0eb3c69aa97977513328e1be33aa0de64b639a41df2bf69bd2203` |
| 发布登记 | `HARNESS/releases/registry.json`，active_version=6 |
| 协调配置 | `HARNESS/site/workspace.local.json` |
| 冻结协调源码 | `HARNESS/artifacts/held-perception-repair-20261111/coordinator-7d135c2748b0` |
| 本地部署配置 | 同产物目录下 `f1-llm-7d135c2748b0` |
| F1 冻结包 | `/home/user/Bruno/HARNESS/development/7d135c2748b0` |
| V6 发布证据 | [release-validation.json](../artifacts/held-perception-repair-20261111/release-validation.json)、[三端核验](../artifacts/held-perception-repair-20261111/post-activation.json) |

交接时，`HARNESS/src/f1_harness` 的 `.py/.md` 内容与 V6 manifest 对应项没有差异。未实施的新方案不能被误当成尚未发布的现成代码。HARNESS 有大量未跟踪／独立上游内容，不能仅凭根仓库 `git diff` 为空判断内容身份；对照冻结 manifest，保留所有他人改动，不擅自 commit/push/reset。

V6 已修：持物面法向关联随当前 EEF 变化；owner 新场景复核统一粗任务告警策略；允许方块／方孔的预期遮挡交由下游判断；诊断原件为后续阶段预留额度。这些不等于持物证据生命周期已全面修好。

V6 发布时2114项软件、135项冻结重点、60项原控制检查通过；正式零输出回放12次技能、2521条软件输出审计通过。**初始连续RGB-D与SAM真实，后半程视觉／机械反馈及模型程序模拟或回放；它没有覆盖本次坏配对与长暂停组合。** 不能继续拿这些计数作为新故障已修证明。

## 4. 两条必须分开处理的现场记录

### 4.1 更新运行：抓取前零动作失败，尚待深审

- `web-4a8502f30a2d491b8d9074c264cada27`，任务实例 `harness-24db6a08bae238098ce9`。
- 北京时间2026-11-11 15:18:04.303开始，15:20:53.185结束，168.88秒；`error`，`measured_stationary`。
- owner 无 `skill_accepted`、无 `harness_skill_first_publication`，尚未进入抓取；4轮模型响应共153.81秒，没有90秒模型超时。
- R1接近预览 `grasp_height_outside_registered_object`；R2建议抓姿 `scene_sweep_blocked`；R3模型构造80°抓姿后 `bounded_IK_did_not_reach_goal`，关节约束激活、残差约4.46mm；R4主动 `finish(status="stuck")`。
- 首轮 `planner_tool_result.moved=true` 与 owner 零动作证据矛盾，待核对返回字段语义，不能说实际动过。

这里只快查了本地日志，**没有核对RGB-D／视频、没有数值重演**，不能断定几何错误、摆放不可达、模板局限或误拒。先读 [workspace-run](../runs/workspace/web-4a8502f30a2d491b8d9074c264cada27/workspace-run.json)、[timing-report](../runs/workspace/web-4a8502f30a2d491b8d9074c264cada27/timing-report.json)、该目录 `planner/trace.jsonl`、`planner.log`、`collected/owner/harness/trace.jsonl`。

### 4.2 已彻查运行：抓稳后运输失败，再丢持物历史

- `web-7a00951227b041fe8caa431b77bf4a38`，任务实例 `harness-a2c007a6ba0fa5ba5bb1`。
- 首动36.14秒，抓取成功46.90秒，`verify_grasp`成功48.07秒，153.60秒用户提交STOP，最终停稳。
- 确认抓稳后0.247秒即接受固定运输，中间没有LLM；6.784秒内四条候选全被数值检查拒绝，**零运输发布**。
- 候选失败：示教路线IK未收敛约1.192mm；两候选分别右臂Link5／Link4与bodyLink5原生碰撞拒绝；另一侧路线关节2到−94°边界，IK停滞约0.534mm。数值残差不是实测跟随误差，也不单独证明任务物理不可达。

| LLM轮次 | 等待 | 实际作用 |
|---|---:|---|
| R1 | 13.37s | 给实体2D框，调用完整 `localize_and_pick`；本地完成抓取并确认，然后运输失败 |
| R2 | 30.05s | 仅查几何字段／策略，工具0.87s，无新动作 |
| R3 | 28.74s | 提议TCP负X6cm、升高16cm；预览被 `cartesian_motion_attachment_unknown` 拒绝 |
| R4 | 21.67s | `inspect_in_hand`及两次本地补验，13.75s均缺持物证据 |
| R5 | 未返回 | 用户约1.56s后STOP，响应与费用未知 |

抓取成功至STOP共106.70秒，R2–R4模型等待80.46秒。已完成4轮共73044 tokens；模型记得历史抓稳，并非每轮重新看图判断。R2已收到 `object_held=null`；R3仍尝试依赖当前持物的动作。R2重新看见同对象重置无进展计数，却没解除阻塞，属于交接／进展语义问题。

**持物历史丢失的已证实路径：** 抓稳后16.16秒仍held=True；16.87秒 `frame_feedback_skew` 隔离保留历史；16.98秒 `unverified_pairing` 不在错误字符串保留列表，直接 `invalidate()`；17.59秒恢复有效帧时历史已空。后续170份样本held未知，163份为 `insufficient_following_motion`；身份一致、夹爪均100，手物相对位移中位0.302mm、最大0.699mm，没有支持明显滑落的测量证据。冻结代码已复现该状态转移。

90份抓后细节两路SAM均video tracking，右腕非空、score全1；成功感知总耗时中位561ms。这条的主因不是SAM一直没分出积木。V6 owner复核等待0.99／1.31秒，前条11.25／13.59秒，说明该修复实际推进；并非控变量整任务提速对照。运输未执行，因此V5运输终点的面法向修复仍未真机验收。

证据集中在 [v6-runtime-audit](../artifacts/v6-runtime-audit-20261111)：[llm.json](../artifacts/v6-runtime-audit-20261111/llm.json)、[vision.json](../artifacts/v6-runtime-audit-20261111/vision.json)、[状态转移复现](../artifacts/v6-runtime-audit-20261111/vision/history-loss-reproduction.json)。源运行在 `HARNESS/runs/workspace/web-7a00951227b041fe8caa431b77bf4a38`；其导出元数据已完成，大视频仍以F1原件为主。

## 5. V6 交接时拟议的修复

### 首次抓取、本地持续监测、策略恢复分工

1. Astra必要时定位目标、选择抓法，生成／调用包含正常流程和分支的程序。每个小动作不单独再问模型。
2. 本地接近、闭爪、正常提起；从闭爪前保留目标观测，提起前段确认目标离开支撑并随手移动，或连续新观测证明处于夹持区域且已离开支撑。不人为追加重复提起动作。
3. 抓到→直接运输；明确抓空→重新定位、调整抓点、预算内重抓；证据暂缺→本地补看，不能将未知直接改成失败或成功。
4. 持物之后后台持续更新当前状态。历史抓稳只记录曾成立，当前新观测负责继续验证；坏帧不删历史，真实脱离必须触发本地保持／停止旧运输，不能等LLM几十秒后才响应。
5. 目标重新位于可抓区域时本地重定位重抓；人仍拿着目标时不追着抓。新区域／新障碍／需要新策略或已有恢复无效才交Astra。
6. 路径不可行→保留抓取事实、修改运输路线或允许的朝向；**不要回到重复静态抓稳检查**。桶上方才求精确在手姿态和孔口对齐。
7. 正常释放前后持续验证，放置有证据后右移避桶返程。恢复始终保留原阶段，只重算受影响后缀，未知或已发布动作不能重放。

沿已有 `held=True/None/False` 和证据来源实现即可。应把“拒收一份样本”与“持物关系失效”分开设计，**不是再往 `reject_measurement` 添加一条字符串白名单**。同样不能永久锁住held以消除阻塞。

### 频率和性能

- 原控制／反馈循环保持原合同；持物监测按新的独立相机＋反馈到达更新，后台运行，不在每个路点串行等待一次完整几何。
- 复用当前SAM跟踪结果，轻量检查夹持区域／相对位置；完整孔口、平面和精姿态计算按阶段消费，不每次检查是否在手都跑全套。
- 约5Hz是讨论中的轻量监测优化目标，**不是现状、不是上游通用标准、更不是新硬门槛**。当前561ms是整管线计算中位数，不等于真实有效新帧率。指标看源帧／反馈身份、物体脱离→检测→保持请求→实测停稳的时延。
- 单个错帧不自动中断整个程序；持续缺少当前动作所需证据进入有界恢复。复用原局部修正／重抓预算，不额外叠加“三轮4秒”等独立等待层。
- Astra只在需要新目标、抓法、路线或观察策略时调用；一次性提供当前阶段、实际发布状态、已有证据、缺项、具体失败和可续跑接口。读到相同字段不算实际进展。

## 6. 已核查的上游依据：哪些可复用，哪些没有

固定SHA以 [upstreams.lock](../configs/upstreams.lock.json) 为准，不自动 `git pull` 替换：RPent `886b3b274d3dd30bbc15615ea512d65ae90bc8b3`；PyRUA `e8f01dca744421d70684adbbc1713fd4d92f8aca`；ASPIRE `fa00f0ef2bc1d91d594b9aa502c22133c9f538f7`。

| 上游证据 | 正确借鉴 | 不应声称 |
|---|---|---|
| [RPent双Franka抓取合同](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/robots/dual_franka/prompts/system.py#L83) | 动作段完成不等于抓到；看动作返回图、夹爪和深度高度；确认后直接接后继段 | 它已提供可直接搬来的连续本地抓稳检测器；照抄15cm段边界 |
| [RPent控制频率](https://github.com/RLinf/RPent/blob/886b3b274d3dd30bbc15615ea512d65ae90bc8b3/robots/dual_franka/runtime_config.py#L48) | 控制频率和语义检测分开 | 10Hz代表视觉检测或模型频率 |
| [PyRUA执行方式](https://dagroup-pku.github.io/PyRUA-Lean/docs/getting-started/how-it-works/) | 同一Python cell组合动作、反馈、条件分支、本地重试，按需返回图像 | 每个动作必须再调用LLM |
| [PyRUA持物说明](https://github.com/DAGroup-PKU/PyRUA-Lean/blob/e8f01dca744421d70684adbbc1713fd4d92f8aca/src/pyrualean/knowledge/robodojo-novla.md#L108) | 夹爪开度只是线索，须确认目标随手移动 | 仿真开度阈值或Pick.success是真机目标抓稳证明 |
| [ASPIRE证据与恢复](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/AGENTS.md#L78) | 复用新观测，保留已验证关系，遮挡未知，恢复回原阶段，仅改受影响后缀 | 它已有能接F1的自动held三态分类器；System2不参与判断 |
| [ASPIRE观测监控](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/src/cap_harness/agent/session.py#L202) | 异步动作与后台观测；默认空闲2Hz／活跃5Hz | 5Hz就是SAM抓稳分类或LLM调用频率 |
| [RPent抓空误报问题](https://github.com/RLinf/RPent/issues/243) | 自查工具“成功”字段的物理含义 | 开源工具返回success所以无需验证 |

把常规持物判断下沉到本地，是F1为效率做的适配，不能借上游名称宣称现成能力。ASPIRE独立经验验证是跨运行技能验证，不是当前在手传感器。上游经验、模型权重和运动代码继续分开管理。

## 7. 当时的源码地图与实施顺序

均相对 `HARNESS/`：

| 范围 | 优先阅读 |
|---|---|
| 观测接入／场景失效 | `src/f1_harness/live_scene.py`、`scene_pipeline.py`，尤其 `reject_measurement`、`invalidate`、`_context` 和持物更新 |
| 随动证明／历史重验证 | `grasp_tracking.py`、`verification.py`；当前正常提起需独立样本与位移，静止仅可重验证历史 |
| 抓姿／运输／连续提案 | `skill_services.py`、`transport.py`、`numerical_service.py`；从实际失败输入复算，不能只验证名义fixture |
| owner与任务阶段 | `owner_bridge.py`、`engine.py`、`observation.py`；保留已修的统一质量告警策略 |
| 本地宏与恢复 | `astra_programs.py`、`recovery_programs.py`，现有 `localize_and_pick`／`resume_pick`／补验接口 |
| 模型交接／无进展 | `planner.py`、`planner_context.py`、`prompt_contract.py`；修改前核对是否另有agent并行改动 |
| SAM／诊断原件 | `mask_service.py`、`sam_service.py`、`scene_failures.py`；视频已启用，不重新宣称“首次接入” |
| 发布／页面 | `scripts/serve_workspace.py`、`site/workspace.local.json`；UI改动前读F1-4242规则 |

执行顺序：

1. **先核对更新run**：抓取前候选为何拒绝、moved返回与owner证据为何不一致。把它作为新增泛化样本，不能覆盖已证实的持物生命周期缺陷。
2. **P0持物生命周期＋真实脱手**：复现已保存坏配对序列，修样本有效性和任务事实的关系；同轮覆盖静止恢复、抓空、遮挡、明确取走、夹爪变化、身份变化，避免只修正例。
3. **P0路径接续**：复算实际抓取／运输姿态，改善通用朝向和中间点选择；保留真实碰撞／限位，支持模型连续提案，避免永久依赖四个固定模板。
4. **P0减少模型空转**：问题分类指向原阶段；同一程序查询后就处理分支，日志直接提供可行动诊断；没有解除阻塞或动作推进不能重置无进展。
5. **P1轻量监测与耗时**：与精几何解耦，实测新帧率和脱手响应；初始孔口重提示、代理响应头长等待、在飞取消与未知用量分别处理。
6. 冻结候选→对应反例＋完整零输出→实际输出审计→只读启动→部署三端核对→用户真机验收，更新STATUS/TODO和同一实验复盘。

## 8. 当时采用的验收环境与部署步骤

### 验收必须补上本次失效链

- 正常首次抓取／抓空；已确认持物后静止等待，再遇错帧并恢复。
- 遮挡不误报脱手；明确移走不维持held；不能把静止目标在桌面误当已提起。
- 运输零发布拒绝之后，从运输阶段改路线；已发布／未知状态不重复动作。
- 原始新帧与反馈身份独立，不用重复帧伪造连续证明；配对故障恢复不能靠重写时间。
- 连续真实RGB-D经过实际SAM与正式管线；缺少数据就记录缺口，不能用理想held反馈宣称覆盖。人为取走先离线／零输出验证；现场按用户安排，不擅自制造风险。
- 确认日志能回答：物体当前状态、证据来源、当前阶段、为何恢复、是否实际输出、这一轮为何必须问模型。

### 环境

- `HARNESS/.venv/bin/python`：普通软件／工具链／pytest／httpx／bwrap。
- `HARNESS/.venv-numerics/bin/python`：原生FK/IK／数值回放；不假定装了pytest/httpx。
- `HARNESS/perception/.venv-gpu/bin/python`：SAM环境。不要把torch／numpy依赖混装进核心环境。
- 数值运算使用 `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1`；F1/上游路径以当前冻结包、overlay和所用检查入口为准。
- 6000D开发及推理，F1原owner执行；本轮不启动训练或替换BC/RLT。GPU、端口和进程所有者使用前实时核对，不凭旧文档直接停止其他服务。

当时的维护人员在 Projects 根目录运行以下检查。

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
PYTHONPATH=HARNESS/src:HARNESS/third_party/RPent \
HARNESS/.venv/bin/python -m pytest HARNESS/tests -q

OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
HARNESS/.venv/bin/python HARNESS/scripts/check_native_integration.py
```

优先先跑改动对应的测试，完整发布再跑必要全链。Unix socket／bwrap沙箱权限失败应按真实原因处理，不改测试或业务逻辑伪造通过。

重点旧测试：`test_grasp_tracking.py`、`test_phase_perception.py`、`test_phase_live_scene.py`、`test_grasp_observation_recovery.py`、`test_recovery_programs.py`、`test_plan_revalidation.py`、`test_real_bootstrap_contract.py`；新反例应验证实际生产状态转移，而非复制实现断言。

### V6 的发布流程

参考 `HARNESS/artifacts/held-perception-repair-20261111/` 中：

`prepare_release.py → prepare_offline.py → replay_saved.py → run_audits.py → install.py → check_fresh_site.py → validate_release.py → switch_idle.py → verify_activation.py`。

这些是绑定V6／其前版的历史脚本。为新候选建立新产物目录，适配当前base bundle、允许改动集合和证据路径；检查脚本里的旧哈希与版本断言，不盲运行。原始配置／标定保持原始身份，离线只记录路径／传输重定位。

完整回放使用 `scripts/replay_full_owner.py --follower approved-output` 和正式RPC/PyRUA入口，实际F1Owner/Ruckig输出到内存接收端。参考V6的 `localize-final-command.json`、`offline-bindings.json`，再运行运输／抓取／恢复／全链输出审计；模拟后半程仍须单列限制。

发布编号从 `releases/registry.json` 串行登记，不猜“下一个V号”；冻结后不得原地改包。切换前核对任务终态、停止确认与实际owner，不能打断正在执行的用户任务。安装／服务切换与发送START是不同动作，沿当前授权范围执行。

模型连接沿现有6000D代理 `127.0.0.1:7890`，未证实需更换地址；服务19843、SAM19842、infra4242的归属以运行配置为准。无凭据401探针只证明当时可达，不能证明付费生成延迟或稳定性；模型读取超时不能自动重发未知结果的POST。

## 9. 当时的交付约定

交接要求逐条说明最新两次运行的差别、所修生产链路、覆盖的反例与验证层级，并记录首动、抓稳到运输、必要模型轮数、恢复耗时、完整结果和误报。后续版本的实测结果由[当前状态](STATUS.md)链接对应复盘。
