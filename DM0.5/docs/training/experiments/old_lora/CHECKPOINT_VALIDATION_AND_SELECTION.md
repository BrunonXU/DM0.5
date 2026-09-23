---
doc_type: experiment
domain: inference
status: frozen
created: 2026-07-30
updated: 2026-07-30
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [13_DM05_G2_CHECKPOINT_VALIDATION_AND_SELECTION_DECISION.md]
superseded_by: []
related: [../../CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md]
---

# DM0.5 × G2 Checkpoint Validation Method, Evidence, and Revised Selection Protocol

更新时间：2026-07-30 10:50 CST

## 1. 文档职责与当前结论边界

本文解释25K–50K已完成的三轮离线实验：每轮输入什么、模型实际执行什么、输出
从哪里计算、对应应画什么图，以及为什么原先的硬淘汰逻辑需要修订。

当前能够被证据直接支持的结论是：

- 30K在已执行的固定Flow Matching验证中优于35K；
- 35K在30K与35K两名入围者的完整10步Euler动作生成比较中更好；
- 因此35K仍是当前离线Primary，30K是Fallback；
- 但25K、40K、45K、50K没有执行同等级完整动作生成，不能声称35K已经被
  严格证明为25K–50K六个候选的全局最佳。

本结论只覆盖开放环离线验证，不证明真机任务成功率。后续25K–100K统一复测完成
前，不再发布“35K绝对最好”的表述。

## 2. 冻结数据与共同实验条件

880条合格episode在训练前按episode隔离：

- 800条train；
- 80条validation；
- 同一个episode不跨split；
- 34D state/action normalization只由800条train计算；
- validation共90,244个可用观察位置。

所有checkpoint比较必须复用相同的：

- validation episode及观察位置；
- batch分组；
- Flow时间、Flow噪声和生成噪声seed；
- train-only normalization；
- 模型输入合同和`50×34` absolute action chunk；
- LLM SDPA / Vision FlashAttention2 / Action SDPA；
- 官方固定Docker镜像和相同推理实现。

“每episode取16帧或32帧”是指沿一条episode的完整时间轴等分为16或32个区间，
每个区间取中点作为一个独立观察位置。不是取开头连续16/32帧，也不是逐次缩短
输入。每个观察位置包含当前图像、指令、机器人状态，以及数据集中对应的一个
`50×34`目标动作块；50表示一个chunk内的未来50个动作时刻，而不是50个chunk。

## 3. 已完成第一轮：六候选FM Loss扫描

### 3.1 输入

- 候选：25K、30K、35K、40K、45K、50K；
- 80条validation episode；
- 每episode等距取16个观察位置；
- 共1,280个唯一观察位置；
- 每个位置使用3轮固定Flow随机性；
- 每个checkpoint共3,840次样本曝光；
- batch size 4。

冻结位置保存在
`server/evaluations/validation_25k_50k_round1/manifest.json`。

### 3.2 比较原理

本轮不生成最终动作。评估器从验证数据读取真实`50×34`动作，随机采样Flow时间
和噪声，把动作置于Flow中间状态，然后让checkpoint预测局部速度/去噪方向。
预测方向与训练目标之间的误差就是FM loss。

相同观察、batch和seed保证各checkpoint面对完全相同的题目。先在每条episode
内部聚合，再让80条episode等权做macro汇总，避免长episode支配结论。

FM loss回答的是：

> 模型在随机Flow中间状态上，单次局部方向预测是否准确？

它不直接回答：

> 从随机噪声连续积分10次后，最终生成动作是否最好？

### 3.3 输出与历史结果

输出包括每个batch的loss、每条episode的平均/中位/最大loss，以及跨80条
episode的macro分布。历史排序为30K、35K、25K、40K、45K、50K。

原流程把前三名送入第二轮。修订后，本轮只作为数值健康检查、泛化诊断和解释
证据；除NaN、Inf、模型加载失败或明显发散外，不再仅凭FM loss硬淘汰checkpoint。

### 3.4 对应实验图

计划图`docs/assets/13_checkpoint_selection/02_round1_fm_paired.png`：

- 六个checkpoint的episode级分布；
- 相对共同基准的逐episode配对差值；
- episode-cluster bootstrap 95%置信区间；
- 不以仅显示平均值的柱状图代替不确定性。

现有结果尚未计算配对置信区间，图生成前不得标注“显著更好/更差”。

## 4. 已完成第二轮：前三名加密FM复测

### 4.1 输入

- 候选：25K、30K、35K；
- 同一80条validation episode；
- 每episode等距取32个观察位置；
- 共2,560个唯一观察位置；
- 每个位置使用5轮固定Flow随机性；
- 每个checkpoint共12,800次样本曝光；
- batch size 4。

冻结位置保存在
`server/evaluations/validation_25k_50k_round2/manifest.json`。

32相对16只表示时间轴采样更密，并不改变单个模型输入或50步action horizon。
增加观察位置与Flow seed是为了降低由位置和随机噪声造成的排名偶然性。

### 4.2 比较原理与输出

模型执行过程和第一轮相同，仍然只测随机Flow中间状态上的局部方向预测。输出
仍然是batch、episode及macro三级FM loss。历史结果仍为30K、35K、25K。

原流程据此只让30K和35K进入完整动作生成。修订后，这轮属于对FM现象的加密
确认，不再作为部署候选的唯一准入门槛。

### 4.3 对应实验图

计划图`docs/assets/13_checkpoint_selection/03_round2_fm_paired.png`：

- 25K、30K、35K在更密采样下的episode级配对差值；
- Round 1与Round 2的排序一致性；
- 位置采样和Flow随机性增加后，置信区间是否收窄。

## 5. 已完成第三轮：两名入围者完整动作生成

### 5.1 输入

- 候选：30K、35K；
- 每episode等距取4个观察位置，共320个唯一输入；
- 每个输入使用2个固定生成seed；
- batch size 2；
- 部署一致的10步Euler Flow；
- 输出一个`50×34`动作块。

### 5.2 Euler Flow与比较原理

推理从随机动作噪声开始。第`k`次模型计算当前动作状态的速度场，再按Euler公式
前进一步：

```text
action_(k+1) = action_k + delta_t * velocity_model(action_k, t_k, observation)
```

连续执行10次后得到最终`50×34`动作块。这里的10步是生成一个动作块所需的10次
网络计算，不是机器人控制10个时刻，也不表示10Hz。

生成时不把真实未来动作喂给模型。生成完成后，才把预测动作与该观察位置在
validation中记录的目标动作比较。

### 5.3 输出从哪里计算

- 归一化全关节动作误差：预测与目标`50×34`逐元素绝对差；归一化避免大尺度
  关节控制总分；
- 物理动作误差：使用train-only q01/q99反归一化后比较；
- horizon误差：分别观察chunk内第1、5、10、25、50个动作时刻；
- 关节组误差：左右臂、左右手分别聚合；
- 首动作跳变：预测第一个动作与输入时刻当前state的绝对差；
- 速度/加速度/jerk误差：按30Hz分别对预测与目标做时间差分，再比较二者；
- q01/q99外比例：`abs(normalized_action)>1`的输出比例，只表示超出训练常见
  分位范围，不等同于机器人机械限位；
- seed dispersion：同一输入的两个生成seed所得动作块之间的平均绝对差。

本轮35K的完整生成动作更接近validation示范，且seed敏感性低于30K，因此35K
成为当前Primary，30K成为Fallback。30K的FM loss反而更低，直接证明FM loss与
完整10步生成排序并非严格等价。

### 5.4 对应实验图

现有两候选结果只能形成临时图，不能作为六候选全局结论。统一复测后计划输出：

- `04_all_candidates_action_paired.png`：所有候选逐episode动作误差配对差值与
  置信区间；
- `05_action_horizon_and_groups.png`：不同horizon与四个关节组的误差结构；
- `06_generation_stability_gates.png`：首动作跳变、分位范围外输出和seed
  dispersion；
- `07_train_vs_validation_context.png`：训练loss、FM验证和完整动作生成随
  checkpoint变化的关系；该图仅解释泛化，不参与单独决策。

## 6. 原流程为什么需要修订

FM loss适合检查训练目标是否健康，但它只检查随机Flow中间状态的一次局部方向
预测；部署会连续积分10次，局部误差可能累积或互相抵消。已观察到30K的FM loss
优于35K、但35K完整动作生成优于30K，因此尚未建立“FM loss排名足以替代部署
生成排名”的证据。

此外，原结果只报告点估计，没有对同一episode上的checkpoint差值执行配对
bootstrap置信区间，也没有预先定义具备部署意义的最小改善幅度。若候选差距小，
仅按均值顺序淘汰并不充分。

## 7. 修订后的25K–100K统一选型流程

### 7.1 候选范围

100K训练完成后统一比较：

```text
25K, 30K, 35K, 40K, 45K, 50K,
55K, 60K, 65K, 70K, 75K, 80K, 85K, 90K, 95K, 100K
```

原50K运行与50K→100K续训分目录保存；50K只作为一个候选，不重复计数。

### 7.2 Gate 0：完整性门禁

所有候选先检查模型加载、有限`50×34`输出、joint order、absolute语义、
normalization digest和部署attention合同。只有损坏、非有限输出或合同不一致的
checkpoint允许在此淘汰。

### 7.3 Diagnostic：FM Loss

所有候选使用相同manifest和seed计算FM loss，作为训练泛化诊断、异常定位和
最终结果解释。FM loss不再单独决定部署候选淘汰。

### 7.4 Pass A：所有候选完整动作生成

所有通过Gate 0的checkpoint都必须执行相同的10步Euler完整动作生成。第一遍
复用每episode 4个位置、2个固定seed的冻结协议，使现有30K/35K结果可复核。

比较以同一episode、同一位置、同一seed的配对差值为基本单位。必须同时输出点
估计、episode-cluster bootstrap 95%置信区间和episode win rate。只要候选与
领先者的差距仍处于不确定区间，就不得淘汰。

### 7.5 Pass B：近邻候选加密动作生成

Pass A中无法明确分开的近邻候选进入预先冻结的更密位置与更多seed复测。具体
采样数、seed数和具备部署意义的最小改善阈值必须在查看Pass A新排名前写入本
实验manifest，禁止看完结果后调整规则。

最终Primary首先依据完整动作生成的配对结果；FM loss用于解释，不覆盖动作生成
结论。若候选在统计和工程意义上均无法分开，优先较早checkpoint，并保留另一
候选进入shadow，而不是制造虚假的单一赢家。

## 8. 图表目录与证据要求

实验图统一规划在`docs/assets/13_checkpoint_selection/`，创建该目录时必须同时
创建`README.md`，记录每张图的：

- 原始JSON和manifest路径；
- checkpoint集合；
- 采样位置、seed及aggregation方法；
- 生成命令和时间；
- 能支持与不能支持的结论。

计划总览图：

- `01_protocol_and_data_flow.png`：800/80数据边界、三轮历史实验和修订流程；
- `02_round1_fm_paired.png`；
- `03_round2_fm_paired.png`；
- `04_all_candidates_action_paired.png`；
- `05_action_horizon_and_groups.png`；
- `06_generation_stability_gates.png`；
- `07_train_vs_validation_context.png`。

在25K–100K统一复测完成前，后四张结果图保持“待生成”，不得用不完整候选集合
绘制成全局排名图。

## 9. 运行时机

当前50K→100K续训占用8张RTX 6000D。虽然单卡仍有空余显存，但并发推理会争抢
GPU计算、扰动训练吞吐并增加峰值显存风险。为保证训练和验证均可复核，不在正式
训练中途插入评估。

100K完成并释放8卡后，使用8卡并行执行统一评估。16个候选可分两批并行加载，
先完成Gate 0和Pass A，再按冻结规则进行Pass B。训练期间只保存checkpoint、
日志和TensorBoard曲线，不修改当前Primary。

## 10. 真机前的必要边界

离线动作MAE仍然只是在单一记录未来上的开放环模仿误差；另一条合理动作轨迹也
可能被MAE惩罚。因此最终还必须依次通过：

1. 全新进程client合同检查；
2. 离线回放、限位、速度、加速度、watchdog和safe hold；
3. 真机shadow只记录不执行；
4. 低速、可急停的短前缀powered test；
5. 完整任务重复试验。

只有闭环任务指标通过，才能宣布模型选择完成。
