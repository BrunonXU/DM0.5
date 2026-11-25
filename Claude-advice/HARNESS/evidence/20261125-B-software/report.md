# V50 首轮候选的软件验收

H-01、H-02、H-03 已分别提交。软件门票尚未通过。全量 pytest 有三条按旧规格要求 4 秒，候选实际等待 15 秒。依用户决定保留原断言，将差异交给 Claude 审阅。已部署版本仍为 V50，本轮 10 个直立布局尚未启动。

正常程序的零硬件输出回放走完了放置确认和复位。最近一条失败记录仍缺方孔测量，候选在同一帧给出了同一原因。回放、历史漏斗和新真机试验分别记录，M0 待现场验证。[回放输入与回执](replays.json) · [历史基线](../20261125-A2-baseline/report.md)

## 实现和 V50 已有能力

| 条目 | 提交 | 已核对的行为 |
|---|---|---|
| H-01 可选相机流变化 | `1992125` | 通用分支对增减的可选流要求用最新观测重扫原候选，通过后才可首发。共有流身份变化或必需几何流缺失仍返回原原因。头部不作无条件必需流。V50 旧精确观测可没有单一 `geometry_camera`，此时沿用头部、右腕双 RGB-D 合同。 |
| H-02 提交前观测窗口 | `5996b41` | 窗口由 4 秒改为 15 秒。任务原有的 300 秒期限、技能期限、会话授权和 epoch 检查均保留。同一第 6 秒才合格的输入，在 4 秒窗口内零提交，在 15 秒窗口内提交一次。 |
| H-03 只读补验次数 | `b8146c7` | 控制引擎与 owner 对同一检查点独立计数。前五次持物状态未知、第六次有证据时成功；第七次被拒。持续六次未知也会用完预算，过程中动作发布数为零。单请求内部最多两次取证，动作修正仍用原预算。 |

相对 A-2 基线 `56bbddf`，四个运行时文件中新增的 `raise HarnessError` 及拒绝原因码均为 **0**。逐项复核覆盖了运行时差异和原有抛错、结果原因字段。Claude 的审阅原文沿用 main 上的版本。[规格](../../review.md) · [本次代码差异](https://github.com/BrunonXU/DM0.5/compare/56bbddf...b8146c7)

## 软件门票

| 检查 | 结果 |
|---|---|
| 6000D 项目环境全量 pytest | 3,082 通过、3 失败、70 跳过，共 3,155 项。三个失败均在下表列出。 |
| 原生数值环境补跑 | 按用例名逐一匹配全量跳过的 70 项，70 通过。 |
| 原生集成 | 正式入口 `scripts/check_native_integration.py` 62 通过，0 失败、0 跳过。 |
| 定向用例 | H-01 新增用例 7/7，H-02 指定的三份测试 119/119，H-03 定向测试 72/72。定向计数与全量重叠。 |
| 项目布局 | `scripts/check_project_layout.py` 通过。 |
| 正常程序回放 | 初始现场 RGB-D 经过 SAM，Astra 使用已记录的响应，后续执行器与抓取反馈模拟。10 个技能阶段通过，放置回执请求号匹配；7 次内存首发、2,361 个平滑输出采样，全程扫掠审计通过。硬件输出 0。 |
| 最近真实失败回放 | `web-8782…` 第 144 帧未测得方孔，正式对准入口复现原原因，0 提交、0 发布。上一条 `web-bbdc…` 第 149 帧求得 24 个路点，第 152 帧缺孔时保留候选，11 项核对通过。 |
| H-01 指定 V40 输入 | 同一左腕加流样例在 V50 已能重扫。候选同样通过 owner 复核，76 个内存输出采样、140 次扫掠检查，硬件输出 0。新增通用行为由可选流增加和减少用例分别覆盖。 |

本批上传 5 个文件，含报告、三份机器回执和文件清单。原始 JUnit、回放和扫掠回执在 6000D 的 `Artifacts/F1-Harness/20261125-v50-claude-software/` 按 SHA-256 保留。[测试回执](software-checks.json) · [回放回执](replays.json) · [文件清单](manifest.json)

## 保留给 Claude 的差异

**H-02 的三个旧断言。** 两个文件都在 Claude 指定的 H-02 测试范围之外，均按用户意见保留。下列三项都先通过了零提交检查，随后在时钟断言处失败，实测 15 秒、期望 4 秒。

| 文件 | 用例 |
|---|---|
| [test_gate_recovery.py](../../../../HARNESS/tests/test_gate_recovery.py#L39) | `test_missing_observation_returns_before_long_task_budget` |
| [test_gate_recovery.py](../../../../HARNESS/tests/test_gate_recovery.py#L46) | `test_total_budget_exhaustion_is_review_without_submission` |
| [test_placement_observation_recovery.py](../../../../HARNESS/tests/test_placement_observation_recovery.py#L115) | `test_fast_align_handoff_requires_fresh_held_observation_and_never_submits[False]` |

**H-02 的现场验收。** 最近一次运输续行超时 `web-99ce…` 在 4.236 秒出现一帧持物证据，缺少运输所需的桶、孔几何。24 次有双端交回记录的超时中，18 次在超时后至第 15 秒没有归档帧，6 次有帧但几何仍不合格。现有原件无法证实延长窗口后这批运行能继续。另一次 `web-cc4c…` 中，接近与闭爪分别等了 5.634 和 4.685 秒，合格帧获当时的 owner 接受，也通过候选的几何检查。接近成功，闭爪随后因规划候选耗尽而停。[现场时序证据](observation-window.json)

**H-03 的正式任务入口。** 按用户决定只改 `engine.py` 和 `owner_bridge.py` 两处运行时实现。`recovery_programs.py` 生成正式程序时，`evidence()` 循环次数取 `local_corrections`。实际运行编译后的入口，预算 0、1、2 分别自动提交 0、1、2 次补验，默认值 2。因而控制端已能接受第六次，当前正式程序仍会在两次后交回 Astra。原用例 `test_no_unbounded_rechecks_or_rechecking_a_different_skill` 已按控制端新上限更新。[程序入口](../../../../HARNESS/src/f1_harness/recovery_programs.py#L82) · [控制端测试](../../../../HARNESS/tests/test_verification_continuation.py)

## 真机指标

本轮新真机运行 0 次。10 个直立布局的 S1–S9、首动和任务时间、模型调用与等待、非物理拒绝和撤权，以及 M0，均待同布局实测。历史 106 次中有 39 次首动（中位 22.95 秒），19 次确认抓持，6 次运输到位；105 次有收尾的记录均无放置回执，另 1 次在首动后截断。历史非物理拒绝 42 次，均值 0.396 次/运行。已知样本最晚到 V49，无法据此计算 V50 候选的改善幅度。[历史漏斗与分母](../20261125-A2-baseline/report.md) · [两条原因有歧义的实录和现场图](../20261125-A2-baseline/cases/README.md)
