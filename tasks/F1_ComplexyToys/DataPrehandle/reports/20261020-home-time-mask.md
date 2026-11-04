# V1：Home监督与短动作窗口验收

本次把任务终点扩展为“插入、松爪、自动回位”，完成V1 Home和短窗口的预处理验收。本文记录的是2026-10-20训练前的数据阶段，原件和旧数据包保留不变。

15条原件均通过审计。新训练池保留全部旧窗口，并增加Home与末尾观测起点；这解决了监督缺失和窗口浪费，实际成功率仍需训练及现场评估。

后续Full GPU预检和1K训练已完成，见[Full复盘](../../training/experiments/20261020-v1-fullsft.md)。本文后续“待训练/待确认”保留当时验收边界，当前进度以[STATUS](../../STATUS.md)为准。

```mermaid
flowchart LR
    A[15条原始记录] --> B[审核已完成V1 Home]
    B --> C[有效观测与动作分别检查]
    C --> D[保留连续1至50步 / 尾部mask]
    D --> E[新统计与三图EEF包]
    E --> F[确认Full recipe后训练]
```

日期：2026-10-20。当前配方见[V1_FullSFT](../../V1_FullSFT.md)；旧版本Home全部排除的证据保留在[历史核查](20261020-reset-and-tail-windows.md)。

## 1. Home如何纳入

按原始`home_started`、预设、真实发布和`home_completed_feedback`绑定区间；保留`source=home`，不伪装成人工操作。实际训练标签仍是已发布`submitted`经原生FK转换的右EEF目标，不能用完成反馈代替命令。

| 原始Home | 本次决定 |
|---|---|
| 12段V1，有反馈完成事件 | 纳入；共27.8168秒，训练10段、验证2段 |
| `88a4ad17`：旧V0分阶段Home | 排除该Home，保留人工示教 |
| `dd1a0b90`、`65e6693a`：结束录制前未确认完成 | 排除该Home，保留人工示教；不推断一定失败，也不标成成功 |

批准Home可以与相邻人工动作组成连续窗口，前提是同一intervention/epoch、发布完整且无日志缺口。12段中左臂发布目标变化均为0，右臂完成反馈最大单关节偏差0.4111°以内；这是技术完成证据，不代替物体插入成功的人工审核。

已看4段Home的开始/中间/结束主图，能观察到右臂从容器附近返回起始侧。未声称逐帧审核全部动作或验证安全性。[图像](../../artifacts/20261020-home-time-mask/home-head-review.png)；[逐段审核JSON](../../artifacts/20261020-home-time-mask/home-review.json)。

EEF监督学习末端回位及右夹爪状态，不保证冗余7关节逐角一致。右动作mask不变，左臂/左夹爪不增加loss。现有15条也未覆盖“回位→选择第二/第三块”的连续流程，后续需采完整多块示教。

## 2. 时间mask具体做了什么

从合格观测起点逐个检查未来30Hz网格上的实际发布动作，遇第一处无效发布、交还/epoch变化、未批准Home、日志缺口或录制结束即停止。**不跳过断点继续拼接**，也不要求未来每个动作点都有新图。

若得到L个有效动作（1≤L≤50），仅这L个动作是标签；数组尾部补零是存储格式。reader返回bool `[H]` mask；训练batch带bool `[B,H]`。动作维度mask仍独立存在，只有前10维有效。

- 统计：只用训练集真实有效动作；padding和验证均不参与。
- 输入：padding位置的动作及噪声清零。
- Attention：padding位置不作为有效query/key，不能污染有效步。
- Loss：每个起点先按L求平均，再平均batch；不会除以固定50稀释短段。
- 推理：依然生成H50，不需要未来mask；真实标签不足50时离线误差仅计算有效前缀。

训练池L≤5有98窗，占0.75%；本轮按起点均匀抽取，不单独提高Home或短窗权重。最短1步是本轮明确选择，并非所有任务都必须如此。对于含HIL的新数据，还需审核人工纠正来源和混采比例，本次未自动纳入HIL。

## 3. 数据数量与保真检查

| 指标 | 训练12条 | 验证3条 |
|---|---:|---:|
| 旧完整窗口 | 11,791 | 2,965 |
| 新保留窗口 | 13,102 | 3,384 |
| 其中完整H50 | 12,457 | 3,126 |
| 其中短窗口 | 645 | 258 |
| Home观测起点 | 666 | 156 |
| 人工起点、未来含Home的窗口 | 484 | 97 |
| 有效动作步数（重叠窗口重复计数） | 637,776 | 162,343 |
| 新增人工观测起点 | 645 | 263 |

全部16,486个起点占17,273个候选网格的95.44%；不同原始观测15,877/17,241，占92.09%。这两种利用率都不是独立示教数量，也不是成功率。903个短窗口与822个Home起点有交集，不能直接相加解释新增量。

全部旧14,756个完整窗口的图像、状态、动作逐项一致；所有新增有效动作均追溯到原始真实发布，Home均在批准区间，无跨epoch/intervention标签。新训练起点增加1,311（11.12%），不足以替代位置/姿态多样性。

固定验证抽样在新包中重新冻结为每条96、共288窗，含13个Home起点和25个短窗口；它不是旧run完全相同的288个观测。旧新loss不能直接解释为同集模型优劣。

## 4. 96门槛纠错

训练分支改为至少1个有效窗口即可进入训练池；验证分支单独要求至少96个窗口，并抽96个互异索引。未来不足96窗口的验证记录暂时明确报错，需单独设计分布式评估策略；不复制填数。

旧15条均超过96，此bug没有删除旧V1的数据。取消门槛与时间mask是不同改动：前者允许短episode已有的完整窗口进入训练池；后者才使不足H50的起点获得部分监督。

## 5. 验收及边界

CPU行为回归45项通过：37项索引/统计/loss测试，加8项原图像与EEF合同兼容测试。真实`Pi0.compute_loss`使用小型attention替身验证padding NaN隔离、零梯度、有效key隔离、全true mask与原路径一致；没有装载3B模型或运行GPU训练。

真实reader核对全部manifest/hash，123个代表窗口覆盖完整/短窗、人工→Home及Home过程；三图确定性处理、实际随机增强和prompt编码通过。原生FK回译最大矩阵误差训练`9.94e-9`、验证`1.10e-8`以内；统计由训练集重算且训练/验证相同。

验证右臂状态有2.93%的字段值超出训练观测min/max，最大单关节偏离6.435°；保留真实状态、沿既有编码处理，不把验证混入统计或悄悄扩大范围。它提示训练覆盖仍有限，不是数据哈希或FK失败。

尚未验收：完整RLinf worker的GPU前后向、Full参数范围、global128显存与吞吐、优化器保存恢复、Full导出及现场成功率。数据包`formal_training_ready=false`有意保留，CPU通过不能冒充全流程训练就绪。

## 6. 产物和复现

新产物：`artifacts/20261020-home-time-mask/`，包含`home-review.json`、`indexes/`、`dataset/`、`lineage-check.json`、`code-identity.json`、`reader-check/`。

- [逐窗口追溯验收](../../artifacts/20261020-home-time-mask/lineage-check.json)
- [真实reader验收](../../artifacts/20261020-home-time-mask/reader-check/report.json)
- [候选时间配置](../../configs/preprocess_v1_home_mask_30hz.json)
- [候选EEF/图像/prompt合同](../../configs/eef_v1_home_mask.json)

以下从Projects根目录执行；需要已有带NumPy/SciPy/Pillow的独立环境。输出目录必须全新，原件只读；reader还需已有PyTorch、SentencePiece及`F1_TOYS_TOKENIZER`资产。此次使用已有容器CPU环境，未联网、未占GPU。

```bash
python tasks/F1_ComplexyToys/DataPrehandle/review_home.py \
  --raw-root tasks/F1_ComplexyToys/artifacts/20261016-v1-formal-audit/raw-copies \
  --split tasks/F1_ComplexyToys/configs/v1_formal_split.json \
  --output /tmp/f1-home-review-new.json
```

用审核文件运行`prepare_collection_indexes.py --home-review ... --config configs/preprocess_v1_home_mask_30hz.json`，绑定其SHA到新EEF合同，再运行`prepare_formal.py`和`check_formal_dataset.py`。完整参数入口均有`--help`。审核含生成时间，重建时文件hash会变化，不应伪造旧hash。

复用到其他任务时：先定义任务终点，再审核自动控制来源是否属于预期专家行为；重新检查时钟、边界、最短监督长度、mask归约和统计，最后独立验证。不能全局把所有自动Home或所有policy动作当专家标签。
