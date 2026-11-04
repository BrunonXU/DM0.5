---
date: 2026-10-14
task: F1_ComplexyToys
status: offline-candidate-verified-not-training-approved
scope: observation-anchor-and-published-action-decoupling
---

# 当前观测与未来动作解耦：2026-10-14 离线候选验收

本次把“当前输入是否可用”和“未来动作标签是否完整”分开判断。当前图片有效、未来图片有缺口、未来实际发布动作仍连续时，保留当前输入对应的完整动作窗口。原始图片和日志只读；既有 `DAgger/F1/src/f1dagger/export.py` 导出基线未修改。本次产物是可审计的 JSONL 训练索引候选，尚未接入 RLinf 或转换为 LeRobot 数据集。

入口：[预处理 SOP](../README.md)、[构建器](../build_training_index.py)、[测试](../tests/test_training_index.py)、[任务基线配置](../../configs/preprocess_baseline.json)、[相机时间诊断](20261014-timing-diagnosis.md)。

## 1. 已实现的选择规则

1. 对完整原件执行既有 `f1dagger.recording.audit`，验证 journal、图片索引、图片包、发布值与每维来源时间证据。结束前再次审计；源 manifest 哈希必须相同。拒绝不完整、审计失败、shadow、input-check 和 teleop-play 原件。
2. 固定整个原始 episode 的时间轴。原点为第一条原始观测的 `available_monotonic_ns`，第 `k` 个格点为 `origin + round(k × 10⁹ / fps)`；使用整数有理数舍入，不累计周期舍入误差、不在图片缺口后重新起表、不删掉空缺后压缩时间。
3. 每个格点选最近过去的**整组原始观测**，保留其真实图片包地址、state、每路图片接收时间、观测年龄及 discontinuity 信息。本轮保留整组选择，未混入逐相机重新配组或插值。
4. 判断当前输入：观测和 state 新鲜；必需相机为 `head/right_wrist` 且接收时间新鲜；图片包存在；观测属于选中的人工介入。检查从该组最早接收时刻 `capture_started_monotonic_ns` 到 anchor 的全部控制记录，避免用进入人工模式后的动作标注进入之前的图片。
5. 独立在同一全局时间网格取 H 个实际发布动作。每格取截至该时刻已经完成发布的最近控制记录，保留 `submitted` 和每维 `action_source_timestamps_ns`；不以 measured、selected、policy proposal 或人工候选替代实际 action。没有发布的新夹爪目标可以合法保持旧值，因此不按夹爪每维来源时间过旧误删记录。
6. 检查窗口覆盖的**每一条中间控制记录**，不限于 H 个取样点：同 episode、介入 ID、policy epoch；人工来源族；`hil_latched`、powered、submitted 存在、publication_complete；动作向量与来源时间合法；控制记录间隔不超过独立阈值且指令序号连续。`human_hold/arm/hand/arm_and_hand` 之间切换属于同一人工来源族，不能把正常开始/停止操作误当成 policy 切换。
7. Home 既通过 control source 排除，也通过 `home_started` 到完成/取消/停止的事件区间排除；未闭合的 Home 区间延伸至记录终点。窗口不得跨 Home。末端不超过停止事件和最后真实控制记录所支持的时间，不外推最后动作到未知未来。
8. 只输出完整 H 步动作；不足的段尾直接记录拒绝，不补齐。未来图片 callback 缺失或掉帧只是图片质量证据，不自动截断此前有效输入的未来动作。当前输入本身不合格时仍拒绝该起点。

若 raw episode 混入另一个 episode 身份，选择直接失败。源 manifest 还绑定原采集配置与 process 身份；本次不跨文件拼接轨迹。

## 2. 配置和冻结的质量条件

以下值来自现有新鲜度基线，作为本次可复查的显式条件；**不是经过曝光校准、任务效果优化后的最优阈值**。

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `schema` | `f1_anchor_action_index_config_v1` | 配置格式 |
| `fps` | 30 | 动作和 anchor 网格频率，CLI 可覆盖 |
| `horizon` | 50 | 完整真实动作点数量，CLI 可覆盖 |
| `required_cameras` | `head`, `right_wrist` | 左腕不决定当前输入新鲜度；原始左腕数据仍保留 |
| `max_observation_age_ns` | 50,000,000 | 整组 available 到 anchor 最多 50 ms |
| `max_camera_age_ns` | 200,000,000 | 必需图片从主机接收到 anchor 最多 200 ms |
| `max_state_age_ns` | 250,000,000 | 每维 state 来源到 anchor 最多 250 ms |
| `max_command_age_ns` | 50,000,000 | 控制完成到取样点最多 50 ms |
| `max_control_gap_ns` | 50,000,000 | 中间相邻控制记录最多相隔 50 ms |

改变 30/20 Hz 不改变任何 `max_*_ns`。H50 首尾跨度分别为 `49/30≈1.633 s` 和 `49/20=2.45 s`。20 Hz 因实际未来范围更长而可能多拒绝靠近任务结束或 Home 的起点；不能只按窗口数评价哪种频率更好。

八份实际候选在任务配置文件落地前运行，manifest 中保存的是内建的同值显式配置和基线说明；随后新增的 `configs/preprocess_baseline.json` 数值与其一致。用下述命令重新运行会记录该配置文件的完整内容。既有产物不改写补注。

配置落地后，已使用该 JSON 重新运行 073059 的 30Hz/H50 候选，结果保存在 [配置绑定复验目录](../../artifacts/20261014-preprocessing/config-bound-check/073059-30hz/manifest.json)。仍保留 823 个窗口，`anchors.jsonl` 与 `rejections.jsonl` 和原候选逐字节一致；新 manifest 的 `source_config_document` 与配置文件内容一致。

## 3. 原件与真实对照结果

原件来自此前只读保存的四份完整录制：

`DAgger/artifacts/results/20261013-resampling-fix/raw-copies/`

已有基线为此前按图片连续区间导出的完整 50 步窗口数，证据在 [旧对照报告](../../../../DAgger/artifacts/results/20261013-resampling-fix/comparison.json)。这里所有新结果均未人审、未训练，`training_ready=false`。

| 原始 episode | 原始观测数 | 旧 30 Hz 完整窗口 | 新 30 Hz 完整窗口 | 新 20 Hz 完整窗口 |
|---|---:|---:|---:|---:|
| `f1_hil_20261013T060830Z_4015997f` | 848 | 408 | 699 | 450 |
| `f1_hil_20261013T073059Z_be04bba2` | 876 | 627 | 823 | 531 |
| `f1_hil_20261013T073143Z_94a17b20` | 118 | 0 | 0 | 0 |
| `f1_hil_20261013T073200Z_17800fe8` | 904 | 529 | 730 | 471 |
| 三条任务候选合计，不含短 Home 记录 | 2628 | 1564 | 2252 | 1452 |

30 Hz 完整窗口合计增加 688 个，约 44.0%；这里比较的是重叠训练窗口，不能视为增加了 688 次独立示教。3.9 秒记录仍无完整窗口，也没有被认定为任务成功示教。

八份候选及汇总位于任务私有产物目录：

`tasks/F1_ComplexyToys/artifacts/20261014-preprocessing/anchor_action_candidates/`

- [comparison.json](../../artifacts/20261014-preprocessing/anchor_action_candidates/comparison.json)：八份实际运行结果。
- [independent_readback.json](../../artifacts/20261014-preprocessing/anchor_action_candidates/independent_readback.json)：独立读取输出，逐值回查原日志的结果。
- 每份 `<episode>/<20|30>hz/` 包含 `manifest.json`、`anchors.jsonl`、`rejections.jsonl`；拒绝原因保留 stage、grid_index、原始 observation_id 和单调时间。

### 29.28 秒重点原件

30 Hz 新索引共评估 879 个网格起点，保留 **823 个完整 H50 窗口**，涉及 806 组不同的原始 anchor 观测；拒绝 56 个起点：1 个起始控制来源不符合要求、8 个当前观测陈旧、47 个末端未来动作不足。823 不能直接除以 876 当作原始图片利用率；806 也只是这个完整窗口索引实际使用过的 anchor 观测数。

823 个窗口中，**185 个跨过了未来的图片间隔 >50 ms 或 callback discontinuity**，但所有未来动作、中间控制流和时间戳仍通过核对。这直接证明了“未来缺一张图，不必丢掉此前有效观测对应的整串动作”已经在真实原件上生效。它不证明缺失照片被恢复，也不证明多相机曝光同步。

| 原件 | 30 Hz 跨未来图片断点的窗口 | 20 Hz 跨未来图片断点的窗口 |
|---|---:|---:|
| 060830 | 284 | 255 |
| 073059 | 185 | 155 |
| 073143 | 0 | 0 |
| 073200 | 200 | 150 |

### 比较范围与混杂因素

本轮保留了整组观测选择，尚未进行逐相机重新配组，避免同时引入新的视觉对齐算法。但旧导出每段重启网格，新候选在整个 episode 上维持一个网格；新候选将完整动作窗口直接索引到原始控制日志；必需相机默认从三路变为本任务的两路。质量时间上限也从部分随 FPS 变化改为独立显式参数。因此这是明确算法版本之间的对照，**不能把全部增量归因为严格只改一个变量的实验**。

## 4. 验证范围

行为测试 **17 passed**，覆盖：未来相机缺口但动作完整、当前观测过旧、格点之间出现单条无效发布/Home/policy/epoch/介入/执行模式变化、所有格点指令看似新鲜但中间控制流存在 55 ms 空缺、Home 事件阻断、末端不补齐、30/20 Hz 使用相同质量条件、屏蔽左腕只影响必需输入门槛、pre-START 图片不能被后续动作重新标注、审核时间裁剪、不同 episode 拒绝、配置拼写错误拒绝、真实 recorder/sink 产物审计和原件哈希不变。

八份真实候选均在开始与结束时执行原始审计。此外，独立读取生成的 `anchors.jsonl`，未调用选择函数来证明自身输出，逐窗口检查：

- state 与原始对应观测逐值相同；仅选过去观测，年龄满足条件。
- 每个动作等于该格点最近已完成发布的 `submitted`；指令序号、每维 action 来源时间匹配。
- 从观测 capture 开始到最后动作格点的每条控制记录均满足人工、同介入/epoch、powered、完整发布；内部时间间隔不超阈值。
- 无 Home 跨越、未来观测、跨记录终点或补齐标签。

原始审计验证图片包与图像 payload 哈希；本次索引没有重新编码图像，也没有用 RLinf 模型读出训练 batch。上述通过范围是本地离线数据检查。

## 5. 本地复现命令

机器：开发工作站。终端：普通 bash。工作目录：`/home/user/projects`。解释器：此前已验证的 `/tmp/f1-dagger-check/bin/python`。不会连接机器人、运行训练或启动推理。

以下使用新目录 `reproduce-01`。目录若已存在，停止并换一个新名字；构建器也会拒绝覆盖既有候选及 `.partial`。日志在该目录的 `logs/`，索引在 `candidates/<episode>/<fps>hz/`。

```bash
cd /home/user/projects
set -euo pipefail
task_run_dir=tasks/F1_ComplexyToys/artifacts/20261014-preprocessing/reproduce-01
mkdir "$task_run_dir"
mkdir "$task_run_dir/logs"
/tmp/f1-dagger-check/bin/python -m pytest -q \
  tasks/F1_ComplexyToys/DataPrehandle/tests/test_training_index.py \
  2>&1 | tee "$task_run_dir/logs/tests.log"
for task_raw in DAgger/artifacts/results/20261013-resampling-fix/raw-copies/*
do
  task_episode=${task_raw##*/}
  for task_fps in 30 20
  do
    /tmp/f1-dagger-check/bin/python \
      tasks/F1_ComplexyToys/DataPrehandle/build_training_index.py \
      "$task_raw" \
      --config tasks/F1_ComplexyToys/configs/preprocess_baseline.json \
      --mode technical --fps "$task_fps" --horizon 50 \
      --output "$task_run_dir/candidates/$task_episode/${task_fps}hz" \
      2>&1 | tee "$task_run_dir/logs/${task_episode}-${task_fps}hz.log"
  done
done
```

上述循环复现八份索引，各运行打印机器可读的 JSON 汇总；不会自动生成本报告已有的跨版本 `comparison.json` 或独立核对结果。各 manifest 保存源码 SHA、原件哈希、最终有效配置、拒绝原因计数和输出文件哈希，可以核对是否与本报告处于同一实现版本。

## 6. technical、reviewed 与审核模板

`technical` 仅用于技术对照：从原件取得出现过的正数介入 ID，仍逐条筛选真正的人工控制；明确 `human_review_accepted=false`、`task_success_reviewed=false`、`training_ready=false`。不能据此把 Home 记录、操作失败、提示词错误或未审阅轨迹变成专家数据。

外部审核模板为 [configs/review.template.json](../../configs/review.template.json)。模板默认 `human_review_accepted=false`、reviewer 为空、介入和时间范围为空，因此不能直接通过 reviewed 模式。审核文件另存，不能写回原始录制目录。

对已经人工审阅的那份原件，填写：

1. `manifest_sha256`：该原件 `journal/manifest.json` 的实际 SHA256；应与 technical manifest 的 `source_manifest_sha256` 一致。原件或 manifest 改变，旧审核不得套用。
2. `reviewer`、`human_review_accepted`：真实审核者及其明确决定，不由脚本伪造；未批准继续保持 false。
3. `accepted_interventions`：允许学习的实际介入 ID。
4. `accepted_time_ranges_ns`：已审核可用的闭区间 `[[start_ns, end_ns], ...]`，采用原始主机单调纳秒，不能填写相对秒数或北京时间。每个候选从观测 `capture_started` 到最后未来动作格点必须完整位于某一个区间，不能越过裁剪边界拼接。
5. `task_success`：审核确认的任务成功属性；未确认保持 false。reviewed 模式本身也不等于任务成功或模型适配验收。

审核文件完成后，在同一机器和工作目录使用新输出路径运行：

```bash
/tmp/f1-dagger-check/bin/python \
  tasks/F1_ComplexyToys/DataPrehandle/build_training_index.py \
  DAgger/artifacts/results/20261013-resampling-fix/raw-copies/f1_hil_20261013T073059Z_be04bba2 \
  --config tasks/F1_ComplexyToys/configs/preprocess_baseline.json \
  --mode reviewed \
  --review /absolute/path/to/actually-reviewed-073059.json \
  --fps 30 --horizon 50 \
  --output tasks/F1_ComplexyToys/artifacts/20261014-preprocessing/reviewed-073059-01
```

`--review` 的路径必须替换为实际审核文件；上面的示例不会创建或批准它。程序检查 contract、源 manifest 哈希、审核者、明确批准、介入列表和时间范围，保存审核文件 SHA256。即使审核通过，输出仍为 `training_ready=false`，因为模型适配与训练读取尚未验收。

## 7. JSONL schema 与未来 RLinf 消费要求

输出 schema 为 `f1_anchor_action_index_v1`。

| 文件 | 内容 |
|---|---|
| `manifest.json` | 源路径/episode/manifest 和 stream 哈希、builder SHA、配置、审核属性、限制、统计、输出文件哈希 |
| `anchors.jsonl` | 每行一个有效 anchor；原始观测身份、单调时间、state/来源时间、frames 图片包引用、逐路年龄、image_mask、完整 action_window |
| `rejections.jsonl` | 每个拒绝网格点的时间、阶段、原因和 observation_id；保留时间缺口 |

`action_window` 每步包含 `grid_monotonic_ns`、控制完成时间/年龄、command/control stream 序号、人工 source、实际 16D action、16 维来源时间。`action_is_pad` 在本版本全部为 false。`frames` 保留三路原始图片引用，而 `image_mask` 只声明本任务必需输入；当前未应用 ROI 或实际模型 mask 变换。

将来训练 adapter 必须：

- 将**一行**作为当前观测加 H 个动作标签读取，不能把 `anchors.jsonl` 相邻行再当成无缺口动作序列来拼 H 步；拒绝的起点会使相邻有效行之间有真实时间空缺。
- 从 manifest 指定原件的 payload 解析图片引用；保留源 episode 和采集场次分组进行 train/validation 划分，避免同一原始示教泄漏到两侧。
- 按 F1 合同适配 16D 顺序、degree/夹爪 0–100、绝对目标含义，再计算训练归一化；不套用其他机器人统计。
- 实际接入主视角 ROI 和左腕 mask，和推理端共享相同处理。模型不能仅靠这里的 JSON mask 字段就自动屏蔽图像。
- 校验模型动作序列长度、20/30 Hz 实际动作时间间隔、重新推理节奏和部署插值策略。当前索引频率不代表机器人底层控制频率。
- 对真实训练 batch 检查图片/state/action 的来源、shape、归一化和标签时间；完成 RLinf/OpenPI 读取与 loss 路径验收后，才另行评估训练就绪状态。

## 8. 未解决事项

逐路相机配组和接收等待仍沿用原件，曝光时间含义、硬件/传输延迟尚未标定；本轮与相机诊断优先级一致，不猜补偿量。ROI 坐标、具体任务提示词、示教质量和成功区间尚需审核。20/30 Hz 任务效果比较、RLinf 训练接入、推理适配和现场 rollout 尚未完成。

本记录不改变机器人现役版本、不启动动作、不代表实机验证；今后逐相机重配或时间补偿应另建候选和日期记录，保留本版本作为可回查对照。
