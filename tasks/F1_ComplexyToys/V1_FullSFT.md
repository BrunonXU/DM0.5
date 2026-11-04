<!-- doc_type: recipe; status: executed; updated: 2026-10-21 -->


# V1_FullSFT：已执行的训练配方与继续条件

这一轮用现有V1示教学习“抓取→插入→松爪→回位”，从官方base做全参数训练，并加入已审核Home和短窗口时间mask。本文保留实际执行的参数、验收条件和耗时依据，供复现与后续对照使用。

**适用版本：旧三图/state16的Full及其原合同续训。** 2026-10-21已确定下一版修正左侧输入未隔离的问题，改为单臂模型输入并独立重训，不另做输入A/B。新方案见[训练recipe第0节](TRAINING_RECIPE.md#right-only-input-fix)；本文保留历史参数，不把旧checkpoint续训写成已经落实新mask。

曲线、释放生成与最新回位诊断尚未证明1K泛化更好；1K暂作针对性补数据的采集基线。10月21日用户新授权GPU4–7续原数据到2K、保留1.5K，已启动；推理/IK服务保留。当前决定看[STATUS](STATUS.md)，离线与现场限制见[最新Full分析](Eval/V1_FullSFT_20261021_ANALYSIS.md)。

```mermaid
flowchart LR
    A[15条V1 + Home + 时间mask] --> B[官方base / Full / batch128]
    B --> C[1K候选比较与分阶段评估]
    C --> D[有继续收益才恢复原日程]
```

[Why&How阅读](http://192.0.2.1:4243/?doc=v1-fullsft) · [训练数据合同](TRAINING_RECIPE.md) · [待办](TODO.md)

## 目标与对照边界

主要看稳定抓取、运输持物、孔口对齐、完整插入、松爪和自动回位；**回位属于完整任务成功条件**，各阶段仍单列。记录首次IK拒绝前的轨迹，不把到达箱子上方计为插入成功。

长期目标为连续处理第二、第三块及不同形状。当前单块示教不含完整的“回位后选择下一块”转换，不能宣称本轮已学会连续多物体。EEF回位也不保证冗余7关节逐角一致；精确关节复位属于另一项输出/控制合同。

Full128与旧LoRA16同时改变可训练范围、batch、学习率日程、Home/短窗监督、prompt和统计，只能先比较整套配方收益。不能把提升单独归因于Full或时间mask；单变量归因需同版数据和batch下另做LoRA/mask消融，本配方不自动追加。

旧LoRA后期训练loss降低而验证FM升高，且6K后学习率恒为1e-6，因此“继续训练够不够”不能只由步数判断。完整依据见[50K曲线与Full预算来源](training/experiments/20261020-v1-50k-review-full-plan.md)。

## 冻结输入与监督规则

| 项目 | 本次实际合同 |
|---|---|
| 数据与划分 | 现有15条成功V1，不混新增示教/HIL；[冻结清单](configs/v1_formal_split.json)为12条训练、3条验证 |
| 窗口 | 训练13,102窗（645短窗）；验证3,384窗（258短窗），周期验证固定288窗 |
| 统计 | train-only重算；只计真实有效动作，padding与验证不参与 |
| 模型 | RLinf `openpi_rlinf` PyTorch π0.5，官方base重新初始化；不加载旧LoRA权重/优化器 |
| 图像 | 三路有效，主图ROI A；224等比补边，训练随机增强；验证/推理仅同源确定性处理 |
| 状态/动作 | state16；固定当前反馈锚点的右EEF10，相对xyz、rotation6D、绝对夹爪；30Hz/H50 |
| mask | 前10维有效；独立时间mask保留L=1–50真实连续前缀，屏蔽padding的噪声、attention、loss与统计 |
| 运动学 | 原生FK与既有受限连续IK，不因Full训练改变线上求解器或保护 |

精确ROI、增强概率、EEF公式、分字段归一化和mask实现只在[训练recipe](TRAINING_RECIPE.md)维护，避免两份参数表分叉。当前Full训练和推理绑定同一prompt：

> Use the right gripper to pick up the wooden block, insert it into the matching opening of the container, release it, and return the right arm to the starting pose with the gripper open.

15段Home纳入12段具有V1预设、完整发布和反馈完成证据的回位，训练10段、验证2段。旧V0一段和录制结束前未确认完成的两段保留原人工监督。允许同一intervention/epoch内已审核人工→Home交接；未批准Home、交还控制、无效发布和日志缺口截断，不删段拼接。

每个合格观测最多监督50步真实连续动作，遇边界保留前缀L。每个起点按自身有效L平均，再对batch平均；训练L≤5只有98窗（0.75%），未额外提高Home或短窗权重。推理仍输出50步，不输入未来mask。训练至少1个有效窗即可打包；验证每条96个互异起点，不能复制凑数。

新数据比旧训练包多1,311窗（+11.12%）：645个人工起点、666个Home起点，484窗含人工→Home转换。旧11,791个完整训练窗逐项保留；窗口重叠不等于新增独立示教。来源和验收见[Home与时间mask报告](DataPrehandle/reports/20261020-home-time-mask.md)。

## 实际执行的优化配方

配置源：[sft_eef_v1_full.yaml](configs/sft_eef_v1_full.yaml)。以下是本次实验记录，不能据此推断GPU当前空闲；实时资源与部署只看STATUS并现场核验。

| 项目 | 实际执行值 |
|---|---|
| 可训练范围 | Full，3,353,433,872个base参数可训练，无LoRA叶子；视觉、语言与动作相关参数均纳入 |
| global batch | 128窗口/optimizer update；四卡×每卡micro2×累积16 |
| 优化器 | 新AdamW，betas `(0.9,0.95)`、eps `1e-8`、weight decay `0`、梯度裁剪 `1.0` |
| 学习率 | 峰值 `2.5e-5`，warmup100，按冻结2K日程cosine下降，末端 `2.5e-6` |
| 末端核验 | 显式`min_lr_rate=0.1`、`min_lr=null`；1K函数端点约`1.4679017636563742e-5`，恢复不重启warmup |
| 精度 | FP32主参数，BF16计算，FP32归约与buffer |
| 分布式 | FSDP `no_shard`、`use_orig_params=true`、非重入gradient checkpointing |
| 随机性与loader | seed `20261020`，`num_workers=0`；保存数据游标、RNG和增强流；每次读取重新采样增强 |
| 增强所有者 | reader开启合同中的增强；core的`train_image_augmentation=false`防止二次增强 |
| 保存/验证 | 每125步完整保存与固定288窗验证；250/500/625/1K阶段导出并生成评估 |
| 训练停止点 | 实际pipeline在1K留下review标记后结束；2K是已冻结学习率预算，非已经执行的训练步数 |

选择较温和的Full峰值学习率是针对小规模新任务的起始假设，不是已证明的最优值。预检只能验证工程与数值稳定，不能凭少量步数证明任务质量。无动作loss路径的参数不要求每步有非零梯度；实际范围和更新证据见[完整验收](training/experiments/20261020-v1-fullsft.md#full适配与验收)。

## 实测耗时与追加预算

本次四卡Full128热态每次更新中位 **10.368秒**。正式run从北京时间10月20日16:11开始，到19:28完成最后阶段评估，合计约**3小时17分**；包括加载、保存、验证和导出/生成等开销。它替代开跑前的14–30秒粗略预算，原估算来源保留于[50K复盘与Full计划依据](training/experiments/20261020-v1-50k-review-full-plan.md)。

| 工作量 | 按当前吞吐折算的纯训练 | 含开销的参考 |
|---|---:|---:|
| 250步 | 约43分钟 | 阶段启动/保存/评估额外计入，不代表实际250导出时间 |
| 625步 | 约1小时48分钟 | 同上；历史到达时间以日志为准 |
| 1K步 | 约2小时53分钟 | 本轮实际16:11→19:28，约3小时17分 |
| 若从1K继续到2K | 额外约2小时53分钟 | **额外约3小时10分～3小时30分**，受共享负载与评估频率影响 |

1K×128=128,000次窗口抽取，约9.77轮名义曝光；2K约19.54轮。625×128与旧LoRA 5K×16仅在累计抽样数上同为80,000，不能推导学习效果等价。窗口高度重叠，曝光轮数不是独立任务数量。

以上依据仅适用于本次四卡配置。若改两卡、microbatch、分片或同时运行推理，需重新测吞吐、显存和保存恢复，不承诺线性缩放。续训前按[H20任务入口](H20_TASK.md)及infra重新协调资源，不从旧配方表推断当前许可或空闲量。

## 验收条件与已获得的证据

| 环节 | 必须核对 | 本次记录 |
|---|---|---|
| 输入 | 新数据/统计/合同哈希；12/3隔离，旧窗口保真 | [预处理验收](DataPrehandle/reports/20261020-home-time-mask.md) |
| Full接线 | 无LoRA，正确参数范围和梯度，保留mask与共享处理器 | [Full工程验收](training/experiments/20261020-v1-fullsft.md#full适配与验收) |
| 真实batch | 有限loss/梯度，实际global128、LR、显存及稳态吞吐 | 同上；真实四卡前后向已记录 |
| 完整恢复 | 权重、Adam、scheduler、RNG及每rank游标延续 | 预检2→3步；正式训练从base独立初始化 |
| 导出 | Full键集合、有限权重、真实更新及无效投影不变，逐tensor一致 | 实际Policy/reader输入与人工/Home/短窗采样通过 |
| 阶段评估 | 同验证集合、噪声与推理步数；按阶段解释指标 | [1K复盘](training/experiments/20261020-v1-fullsft.md#2026-10-211k完成复盘暂不自动继续2k)列明已完成项及覆盖缺口 |
| 现场比较 | 同推理模式/采样步数/RTC参数，完整任务分阶段判断 | 当前进展只看[STATUS](STATUS.md)与[Eval](Eval/README.md) |

旧绝对关节3+1步smoke不能替代以上EEF/Full128验收；Full导出使用base来源、可训练范围和完整权重一致性，而非LoRA的冻结权重断言。模型交付仍绑定统计、ROI、prompt、mask、FPS/H、运动学、代码与依赖版本，不只替换权重文件名。

## 继续训练的判断与监控

固定288窗的FM loss与生成动作误差分别报告。新集合、统计和prompt已经改变，不能与旧LoRA loss直接作同集比较。短窗只计算真实有效标签；全H50指标只对L=50统计。

后续已补释放固定输入对照及新1K回位对照：1K训练释放点拟合改善，留出释放误差未改善；回位自身继续偏离，RTC后段放大趋势。训练包含Home，但终点保持覆盖短。详见[Full阶段分析](Eval/V1_FullSFT_20261021_ANALYSIS.md)；这些预测检查仍不是完整任务成功率。

若1K现场表现和扩大后的阶段评估仍有收益，可讨论从完整断点先续到1.5K，再判断是否到2K；保持原优化器和2K学习率日程。若两者在同一阶段或位置变化下同样失败，优先补对应示教/纠正。不能以最低FM自动选模，也不能以训练loss继续下降自动延长。

10月21日11:33按新授权使用[GPU4–7续训入口](scripts/resume_v1_full_1000_to_2000.sh)，目标由候选1.5K调整为2K，保留1.5K。仅修改物理卡映射与runner停止步数，原四rank、global128、Adam/RNG/采样游标、原2K学习率日程和W&B run保持；不重启warmup。每125步保存/验证，结束后导出1500与2000，不自动部署。按既有吞吐追加1K约3小时10分～3小时30分，实时记录见[Full实验](training/experiments/20261020-v1-fullsft.md)。旧[1500候选入口](Eval/artifacts/20261021-assisted-release-home/continuation-review/resume-to-1500.sh)仅留历史，不再执行。新增DAgger数据属于下一版实验，不混入此次原数据续训。

每次smoke、预检、恢复检查与正式训练均接W&B。正式恢复沿同run保持step连续；工程检查使用独立run。日志记录数据/代码/recipe身份、实际LR、loss、梯度、吞吐、checkpoint及生成评估。网络不可用时明确offline并留同步步骤，不为补监控重跑或编造指标。

本轮正式组`03_V1_FullSFT`；工程检查`00_Engineering_Checks`；历史单条和15条LoRA分别为`01_SingleDemo_LoRA`、`02_V1_LoRA`。job type区分train/preflight/resume-check，tags记录Full、B128、Home、TimeMask。run链接及分类回执见[实验报告](training/experiments/20261020-v1-fullsft.md#wb分类)。

冻结源码、数据和checkpoint路径集中在[实验版本表](training/experiments/20261020-v1-fullsft.md#版本与运行入口)。开发维护仍在6000D，H20只同步不可变release；原件、旧数据包和在线bundle不覆盖。训练时RTC、连续多物体和新增HIL不属于本轮已经完成的内容，时间mask的单独收益仍需独立对照。
