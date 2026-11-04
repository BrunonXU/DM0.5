<!-- doc_type: recipe; status: canonical; updated: 2026-11-03 -->

# F1积木：当前训练 recipe

真实人工执行不再自动等于专家监督。expert_approved只来自有源审核区间；缺省为false。可信人工转移仍用于critic，actor仅对获批动作做BC，其余保留VLA参考正则和Q目标。评测身份在采集前登记，并从RL、SFT和额度中隔离。

已完成同BC400起点的清洁BC／Q对照与13条原始决策缓存重建，动作、特征及奖励逐张量一致。它尚未证明自主任务改善；开发集拟合与真机任务级评价分别报告，详见[结果及后续验收](inference/experiments/20261103-continuity-validation-V44.md)。

## V45：交互、结果和专家审核独立

适用范围：2026-11-03已部署控制V45／独立学习组件V46，Windows操控包待现场安装。下文带日期的旧数据量、模型和操作语义均为历史证据，当前事实以STATUS为准。



**以下为V41历史结果，已由本轮清洁分支替代。11月3日11:01条已撤回至干净1024／256；随后12:42条新增78转移，学习器自动更新到1336步／334次Q，最新发布1328／332。** 当前局部595训练／251验证、DAgger179／22；12:44条未进入RLT，不产生局部训练。12:42未↑而Y结束，保留失败0；用户回忆倾向失败但未完全确认，不把它升级为已确认失败验证。实际准入、排除和版本见[两条审计](inference/artifacts/20261103-v42-two-rollouts/training/audit.md)。已统一离线／在线专家采样、修复参考尾部约束；自主段使用原始actor输入／输出及真实发布前缀，人工使用下一拍目标，Home和撤离尾段不作为放置目标。旧2192分支已停，原件与历史权重保留，下文1024对照和517训练数量是新增本条前的固定实验基线。

最终1024步同采样对照：原约束Q姿态MAE2.847°，前缀＋尾部Q1.406°，再加0.1保守critic正则Q1.009°；初始BC400为0.729°，同数据BC为0.862°。释放召回均57.51%，critic校准仍不充分。这里的“释放召回”是6条开发验证原件中193个人工张爪采样点的离线开爪侧命中率，当前三组复算均111/193，**不是真实释放或放置成功率**；按原件等权平均为42.30%，相邻点高度相关。定义、来源与选模暴露见[指标复核](training/experiments/20261102-rlt-rtc-V41.md#release-recall-audit-20261103)。修复减轻偏转退化，尚未证明自主完成率改善；不能保证继续HIL更新会单调进步。配方、W&B与边界见[V41报告](training/experiments/20261102-rlt-rtc-V41.md)。

第一阶段继续冻结B1250／encoder，仅小actor／critic更新。共用50%专家池（旧DAgger＋局部人工）、50%局部池；真实人工前缀保持原BC权重，未执行尾部仅以冻结参考作正则。新RTC数据另带固定承诺前缀和下一次计划决策长度，actor／TD查询不再默认全C16；原同步数据默认16，混合时只在内存补字段。所有原始奖励、实际执行长度与终态保留。

独立官方runner从1024／256恢复到1028／257的预检已结束，1028保持隔离。当前runner使用`runs/f1-rlt-rtc-V41/retraction-20261103-51a391be/online-config.json`，健康状态读取同目录`health/actor.json`；原`ready-data-r2/health/actor.json`是已停止分支的历史状态。发布已开启，快照仍在`ready-data-r2/snapshots`；[在线W&B](https://wandb.ai/robotics-example/f1_complexytoys/runs/f1rtc20261103retract51a391be)分类`18_RLT_Objective_RTC_V41`／`online-local-actor`。历史517训练转移不重复增加额度，新增有效训练转移每条增加4步critic预算、actor每4步更新、每16步评估同步。验证条不训练；批准监督试用不等于超过基线。

F1优先包、6000D上传器、H20新adapter及GPU3 RTC推理已成套切换。首次真实传输用已留出的历史包，52.4秒到达H20，重建仍49个验证转移、训练额度不变；包清单的旧58窗口计数不等于新决策转移。11:01实录已产生27个有效训练转移（11个actor决策、16个人工窗口），触发108次critic／27次Q更新，并发布1120候选；随后用户明确废弃整条，旧分支停止，使用源SHA绑定排除和新派生目录，从采集前1024完整恢复，不能仅删除buffer后沿用1132权重。下一条仍需验候选实际采用和任务效果。学习器和推理重启路径、Ray切换顺序见[V41部署记录](training/experiments/20261102-rlt-rtc-V41.md#deployment-20261103)。

**奖励与准入分别判断。** ↑关闭局部任务并给终点奖励1；正常START交回VLA或Y关闭尚未成功的局部任务，终点奖励0，也可进入RL。异常中断待审核，用户废弃条明确exclude；Home、保持、未执行尾段不作交互转移。人工辅助成功不算actor自主成功，未按↑不能凭完整录制已保存推断成功。撤回与版本证据见[V42复盘](inference/experiments/20261103-rlt-rtc-V42.md)。 中间奖励0仍有后续价值，局部失败终点0则终止自举；这与负奖励、硬保护或“每次人工接管自动惩罚”不同。↓无评分功能，人工救回并↑记辅助成功；成功漏↑必须审核修正，不能当真实失败对照。

以下日期段落保留当时配方；其中“未部署”“Q关闭”和旧额度只适用于对应历史时点，当前以页首V41及STATUS为准。

**V39的完整动作数据迁移与三组独立对照已完成，未切生产。** 新表示保留13条局部634／234、DAgger179／22，critic终点23／1；从BC400及新critic各训练256步，BC姿态MAE0.61°、Q1.92°、Q旋转投影2.22°，释放召回均55.92%。新SO(3)指标已纳入候选评估，旋转投影仅为默认关闭的实验选项。不能把动作迁移成功当作Q效果改善，见[V39结果与W&B](inference/experiments/20261102-rlt-repair-V39.md)。

下一步先对齐真实actor决策obs、请求、已执行前缀、实际耗时和终态，再切独立正式分支；新数据配置仍保留旧生产目录字段，不能直接拿它启动runner。原件、旧缓存、2192权重和线上训练保持，候选检查点未发布。

11月2日12:19条已自动触发正式2192／548更新，验证夹爪误差小幅下降、释放召回未变。晚按↑的尾段现已按用户授权重裁：104→95转移、实际buffer634／234，原件保留；2192的历史梯度未撤销，旧动作语义仍未生产迁移。数据结构有效与学习目标正确分别验收，见[本条审计](inference/experiments/20261102-latest-rollout-audit.md)。

**2026-11-02：正式学习器保全1776 critic／444 Q actor更新恢复，W&B真实历史已补齐。** 局部buffer539训练／234验证，旧DAgger179／22，B1250／encoder冻结；局部优先包与完整原件以同一源修订去重，重复传输不增加训练额度。原两条自动续训与实际轨迹见[数据审计](inference/experiments/20261102-two-rollouts.md)，本轮迁移及监控证据见[V34整改](inference/experiments/20261102-framework-repair.md)。

**新动作表示仅完成独立诊断，尚未用于正式生产训练。** 旧合同使用平滑后submitted，新合同使用有真实发布证据的平滑前selected；recipe／replay／示范／终点须一致，跨表示不直接恢复critic。新合同实际耗时仍进入SMDP折扣，未来未知耗时不作为actor查询Q的已知输入。四组256步BC／Q对照显示Q真实参与但误松增加，实验权重未发布；完整chunk锚点、旧种子／终点及生产分支迁移仍待完成。

此候选仅通过人工监督试用条件，critic校准和真机自主成功改善未通过或未证明；数据入库、梯度更新、快照发布、局部实际采用是四项不同证据，不能只看学习器存活或loss。

**在线RLT必须验收“新控制来源→切片→实际入库”，不能以学习器在线代替新数据被消费。** 10月30日四条人工辅助成功被新增`human_compute_wait`的reader兼容遗漏挡住；应按暂停切片并版本化重建，不当专家动作、不重复发额度。此外，更早漏↑成功条的错误奖励已参与更新，需源绑定审核及独立训练对照，单改缓存不能抹去历史梯度影响。当前Q已开启，运行步数以[STATUS](STATUS.md)为准，证据见[四条实录复盘](inference/experiments/20261030-four-rollouts.md)和[全量数据审核](inference/experiments/20261027-rlt-local-v2.md#data-audit-20261030)；下文日期配方保留当时设置。

**当前模型已完成单臂输入修正和V1＋九条DAgger纠正Full训练；10月26日优化版B1250首次获得用户确认的完整任务成功，稳定性仍待复验。** 此前四个checkpoint首轮均未完成任务，无RTC引导停滞与释放偏晚的历史证据见[DAgger复盘](Eval/V1_DAgger/README.md)，不能以一次成功撤销数据覆盖与释放判断问题。

**阶段采样对照已完成，尚未形成更好的默认recipe。** 同一新DAgger1K各续250步：原采样A几乎不改善松手，松手临近加权B有局部帮助但出现更早误松倾向，Home没有明确改善；固定验证loss两组均上升。保留当前输入/监督合同，不自动采用4倍加权或继续堆步数，下一步核对释放触发与针对性数据；见[实验结论](training/experiments/20261022-stage-sampling.md)，运行事实看[STATUS](STATUS.md)。

2026-10-23[释放链路与100步拟合诊断](DataPrehandle/reports/20261023-release-supervision.md)已完成：抽样未见错配，明确训练事件能拟合，静止等待帧及独立事件误松增加。诊断关闭增强、global16和新Adam仅用于检查可拟合性，不升级正式recipe；后续先细化释放监督/布局覆盖与独立验证，不盲目续训。

本页维护共同训练合同；[V1＋DAgger配方](V1_DAgger_FullSFT.md)保存父训练的冻结设置，第0节记录已落实的单臂修正，第1–6节中旧版数据量/观测合同保留历史语境。旧Full/LoRA仍绑定原处理器，不与新checkpoint混用。

**10月29日RLT常驻Q试用已接通。** 冻结B1250与encoder；旧DAgger179／22与新局部142／48分别保留训练／开发验证，专家池与在线replay各占半batch。新run从部署incumbent actor＋修正568步critic启动，Q=0.1、BC=5、固定噪声std=0.002；critic每步更新、actor每4步更新。历史种子仅提供一次128步预算，新增训练转移各增加4步；每16步评估和同步快照。实际128／32已完成，候选通过有限值／IK试用筛查但未证明改善。完整配置为[在线Q recipe](configs/rlt_online_q_20261029.json)，[恢复、W&B及准入解释](inference/experiments/20261027-rlt-local-v2.md#online-q-20261029)。

**10月26日RLT已完成五条高估诊断及九条成功人工纠正预热。** B1250和encoder冻结，新小头550步早停、保留400步BC候选；critic留出成功/失败仍倒序，未启用Q引导。BC400此后已部署固定推理，异步框架已进入实现与验收；不再沿用早先“暂缓接入”的计划。主训练W&B分类`09_RLT_Stage2_DAgger_B1250`，预检单列；预算、依据与结果见[人工纠正配方](training/experiments/20261026-rlt-dagger-success.md)，[首轮token与五条试训](training/experiments/20261026-rlt-frozen-b1250.md)保留历史。

原始录制 → **时间对齐与质量筛选** → 生成20/30Hz训练序列 → ROI、相机mask、动作适配与归一化 → 构造动作窗口 → 训练。

上面是可复用流程；**本轮实际采用30Hz**，未运行20Hz训练对照。[预处理SOP](DataPrehandle/README.md) · [推理recipe](INFERENCE_RECIPE.md) · [Why&How阅读](http://192.0.2.1:4243/?doc=training-recipe)

<a id="right-only-input-fix"></a>

## V34传输及保全恢复配置

正式H20 run内的`production-V34-config.json`启用`priority_root`，入口`start-production-V34.sh`。新恢复点`transport-resume-V34/global_step_1776/actor`仅显式迁移代码／缓存身份，全部模型、优化器、RNG、incumbent和额度保持；12个缓存学习张量逐位一致。优先包哈希与原件/审核身份相符才提交，原件随后到达复用同缓存。

独立动作合同对照使用 `scripts/rlt_controller_contract_trial.py`，W&B group为`15_RLT_Controller_Contract_V34`，不向正式快照目录发布。正式动作表示仍为`submitted_relative_eef10_v1`；新`approved_reference_relative_eef10_v1`不能与旧缓存或critic混用。完整结果及限制见上方整改报告。以下日期配方保留当时状态，当前运行以STATUS为准。

## 局部 RLT 异步配方（2026-10-28，候选发布已接通）

当前使用 [rlt_local_online_20261028.json](configs/rlt_local_online_20261028.json) 和 [数据准入清单](configs/rlt_local_data_policy_20261028.json)，旧 `rlt_b1250_local_async.json` 仅保留历史。冻结B1250与token encoder，从BC400初始化actor、新局部目标critic开始；不恢复故障条132步或两条混合8步工程预检。复用固定RLinf AsyncEmbodiedRunner，CPU运行小头，GPU1既有服务提特征和推理。

10:42条71转移训练，11:11条48转移开发验证、无梯度，09:41故障条排除。未来符合冻结base／encoder及本机协议的新episode按ID哈希预先划分约80%训练／20%验证，不按结果挑选；异常及身份不符隔离。正常→至↑的局部段包含人工接管；正常未↑退出为终止失败，断网／急停待审；只学习有效真实动作及终点，图像断点、未执行chunk尾段、撤离和Home不补造。

训练率1e-4、batch64、每步actor更新，当前不启用输入标准化对照、不增加松手样本权重；每个新增训练转移最多授予4个更新额度，验证和重复接收不授予额度。无额度时runner等待新数据。每10步验证／尝试发布、每25步保存恢复点，合格候选另存完整学习器检查点。新候选需动作有限、有人类释放／持物覆盖、动作与位置误差不超过base及当前候选各自105%、夹爪误差较base至少改善5%且不超过当前105%、释放召回不降且大于0、持物误松不增、抽样原生IK不新增拒绝。门槛是工程筛选，不是统计显著性或真机成功证明；开发验证已用于选模，后续需要新留出尝试。

Q引导保持关闭，critic训练不等于已经能够指导actor。新版本只在下一次→生效，段内固定；失败候选保留证据但不推进latest。W&B正式分类 `10_RLT_Local_Async_B1250`，对照分类 `11_RLT_Local_Actor_Repair_B1250`，当前运行链接、来源追溯、恢复和实际指标见[本轮验收](inference/experiments/20261027-rlt-local-v2.md#iteration-20261028)。旧九条纠正已通过BC400继承经验；10月29日另完成释放终点重裁，得到179训练／22验证的独立种子张量，保留DAgger采集身份、排除撤离/Home，尚未加入常驻buffer。新种子与142／48局部数据完成Q开关对照（各128步critic预热＋128步联合、每4步更新actor、50%旧示范采样），Q开启组32次Q更新未改善释放且误松增加，未推广；W&B分类`12_RLT_Local_Demo_Joint_20261029`／`offline-q-ab`，详见[配方与结果](inference/experiments/20261027-rlt-local-v2.md#demo-joint-20261029)。

## 0. 重大缺陷：左侧输入未隔离；下一版单臂方案（2026-10-21）

### 已核实的问题与影响

现有`action_loss_mask`及`mask_inactive_action_inputs`屏蔽的是动作噪声、更新与loss；它们不屏蔽观测。旧冻结版本的训练和推理处理器将完整state16归一化、编码成文本token，三路`image_mask`全为true。当前开发版已在共享[处理器](src/f1_toys/processing.py)按`right_only_v1`修正；旧模型服务仍绑定原合同。右动作的loss仍可通过共享网络训练模型利用左侧观测，不能把“左动作不计loss”写成“左侧信息不影响右动作”。

[10月14日候选](DataPrehandle/reports/20261014-anchor-action-index.md)曾要求接入左腕mask，[10月15日smoke](training/experiments/20261015-v1-smoke.md)转为三图加state16；本次未找到这一转向的充分理由或明确确认记录，也未找到最初要求屏蔽所有左侧数值状态的直接记录。缺陷属于本任务输入选择与隔离不完整，不等于已有右EEF监督或loss计算错误。

本次只读核对现有训练13,102窗、验证3,384窗：左夹爪全部为0；训练左7关节各自总变化范围均小于0.65°，离散后仅1–2个bin。三个实际图像预览中左腕未见任务木块/孔，抽样不能代替全15条覆盖审查。数据、代码哈希与限制在[核对回执](artifacts/20261021-right-only-input-audit/summary.json)。没有证据证明左侧输入造成现有任务失败，也不承诺删除后成功率必然提高。

### 最终输入合同

| 层 | 下一版确定规则 |
|---|---|
| 原始数据与物理状态 | 原件只读，保留真实16D顺序、单位、来源和时间戳；不把左侧实测值原地改成0 |
| 模型状态 | 有效信息仅右7关节与右夹爪；保持32维布局，`active_state_indices=[7,8,9,10,11,12,13,15]`，这些位置按右侧规则归一化，其余位置在tokenizer之前统一置0 |
| 状态隔离含义 | 左侧原值不能影响state token或其他模型条件；固定0仅为模型占位，不是实测反馈、HAL目标或逐token attention mask。不能把EEF的前10维动作mask复用成状态mask |
| 模型图像 | `head`、`right_wrist`有效；`left_wrist`使用固定占位图且`image_mask=false`，不把真实左腕图作为条件。仅换黑图而mask仍为true不合格 |
| 图像几何 | 主图沿用ROI A、右腕全图、224等比补边；训练随机增强仅处理有效图，验证/推理确定性一致。主图仍含左夹爪外观，不宣称已去除画面中的所有左侧像素 |
| 动作与执行 | 右EEF10、固定原始右臂反馈锚点、30Hz/H50、动作维mask、时间mask、RTC和原生FK/IK保持现有语义；控制器所需真实左臂状态与保持目标另行保留 |
| 版本绑定 | 新建输入合同、统计、数据manifest及bundle身份；明确有效状态索引和相机mask。原冻结JSON/数据包/旧模型处理器不覆盖 |

选择保留槽位是为了减少无关接口变更；模型虽然仍用32维容器，实际只得到8维右侧状态信息。训练与推理必须调用同一个状态处理函数，覆盖普通图像入口和`prepared_pixels`旁路，在tokenization之前完成隔离；只改`Observation.state`而保留旧state文本token不算修复。原始状态继续供FK、IK及控制检查使用。

### 重训、数据和对比的决定

1. **新建任务Full微调，从已验证的官方π0.5预训练base开始。** 不从随机权重重做基础预训练；也不将旧任务checkpoint/Adam状态直接当作新输入版本的断点恢复。采用新run、全新优化器/调度器/RNG与数据游标；以后只在新合同内部恢复。
2. 已有合格示教仍可复用，按新合同生成派生包，不因本问题要求全部重采。已审核的完整示教和HIL纠正可纳入下一版，但逐条审核、来源/监督区间、episode/场次隔离与混采比例必须先冻结；尚未审核的数据不自动进入训练。旧12/3来源划分不得泄漏。
3. 起始优化参数沿用已验证Full/global128和2K学习率日程的量级，实际步数、数据清单与保存/阶段评估点在开跑前冻结；不把旧2K续训视为本修正的训练，不据“输入变少”承诺更快收敛。具体资源按STATUS与实时占用核对，本次文档决定不启动或中止既有进程。
4. **不做“旧输入 vs 单臂输入”的额外训练A/B，也不再分别训练删状态/删相机的消融组。** 本轮以单臂任务需求和减少无关依赖直接修正；缺陷验收与任务效果验收仍必须做，不能据无对照结果声称性能改善全由mask带来。
5. W&B新正式group定为`04_RightOnly_FullSFT`、`job_type=train`，带`f1_complexytoys/right_only/state_mask/left_wrist_mask`标签；工程预检仍归`00_Engineering_Checks`并用独立run。记录数据/输入合同/代码身份及loss、学习率、梯度、吞吐、checkpoint。新正式run不得接写旧`03_V1_FullSFT`的step。

### 必须通过的验收（不是额外训练对照）

- 对固定右侧观测独立改变合法左臂/左夹爪值和左腕图；训练处理对照固定同一augmentation_seed/RNG draw，推理保持确定性处理，扰动前后模型state、文本token、有效图及mask必须相同。在模型评估模式、相同采样噪声下，右EEF预测应在同运行时确定性容差内一致。比较模型有效输入与右输出，不要求物理诊断字段或左侧held目标相同。
- 对真实样本核对三key中的两路有效图、右侧状态顺序和范围、标签/反变换；普通入口与prepared-pixels入口一致。左图attention排除必须进入eager及实际编译/Graph路径；固定占位仍可能消耗视觉编码计算，不预先宣称提速比例。
- 保留动作维mask与时间mask回归、有限loss/梯度、保存恢复与bundle导出检查；随后历史回放、真实相机零输出shadow、现场抓取→插入→释放→回位验收，不能以loss变低或IK通过替代任务成功。

**整套左夹爪拆卸是独立的运行时兼容条件。** 本修正先消除模型对左侧数值和左腕图内容的依赖；原采集/网络/控制入口仍可能要求三图、完整反馈和左右夹爪命令。允许硬件缺失必须另外更新能力声明、真实性/时效检查、发布与录制schema，不能用伪造0反馈绕过保护。具体见[推理端适配方案](INFERENCE_RECIPE.md#right-only-input-fix)。

## 1. 旧版实际合同与沿用的任务文本

| 项目 | 已执行Full合同（新输入按第0节替代） |
|---|---|
| 后端 | RLinf `openpi_rlinf`，PyTorch π0.5；官方base重新初始化，无LoRA注入 |
| 训练数据 | 15条成功V1，同物体/任务；冻结12条训练、3条验证，按原episode隔离 |
| 监督目标 | 右手抓取、匹配孔插入、释放、张开夹爪回起始末端位姿 |
| 观测 | 三路224×224 RGB + 实测state16；三路image_mask均为true |
| 动作 | 右EEF10：相对xyz3、rotation6D6、绝对夹爪1；内部补至32维 |
| 时间 | 30Hz、H50；真实连续前缀L=1–50，按时间mask监督 |
| 统计 | 当前训练集重算；验证、padding不参与；模型包绑定同版统计 |
| 边界 | 当前为单块任务；不据此宣称已学会下一块选择或连续多物体 |

训练和对应Full推理包使用同一prompt：

> Use the right gripper to pick up the wooden block, insert it into the matching opening of the container, release it, and return the right arm to the starting pose with the gripper open.

动作/处理合同见[EEF Home/mask配置](configs/eef_v1_home_mask.json)，数据使用`f1_toys_eef_dataset_v3`，动作合同schema仍为`f1_toys_eef_contract_v2`。内部动作32维、有效监督10维、外部硬件16维分别验收，不能把某个`action_dim`当成三者共同含义。原始state16顺序为左臂7、右臂7、左夹爪、右夹爪，关节为HAL度数、夹爪0开/100合；模型观测归一化后再补16个零。

冻结配置内候选期的`prompt_status`、`supervision_authorization`文字保留为版本来源，不作为当前部署或授权状态。不得为更新文字原地改写已被数据/模型哈希绑定的合同；运行事实见STATUS和对应回执。

## 2. 时间对齐、Home与短窗口监督

当前索引采用最近过去的整组输入与最近完成发布的动作；输入有效性和未来标签连续性分别判断。原始action取有效`submitted`，state取实测反馈；不以反馈补动作，不删除坏段后拼接两侧，不使用未来关节真值作在线IK参考。具体时间质量门槛、证据与诊断方法见[预处理SOP](DataPrehandle/README.md)。

自动Home仅纳入审核通过、V1预设、连续有效发布且有反馈完成证据的12段。旧V0一段和未确认完成的两段仅保留原人工监督。同一intervention/epoch中已批准的人工→Home交接可连续学习；其他来源边界、交还控制、日志缺口或无效发布仍截断。

训练包13,102窗，含645短窗；验证包3,384窗，含258短窗。旧11,791/2,965个完整窗逐项保留；新增666个Home起点及645个人工起点，484个窗口含人工→Home转换。这些重叠窗口不是新增独立示教，来源与逐项验收见[Home/mask报告](DataPrehandle/reports/20261020-home-time-mask.md)。

时间mask要求从第0步开始的非空连续真前缀。存储补到50步，补齐位置不提供动作/噪声输入，不参与有效token的attention，也不计loss或统计。每个起点先按自身L和有效10维求平均，再对batch求平均；不按32维或补齐后的50步稀释。旧V1训练按全池起点均匀采样；本轮V1＋DAgger按来源75∶25、来源内episode和起点均匀采样，未额外放大短窗或Home权重；推理仍预测H50，不输入未来标签mask。

**训练96窗口门槛已移除**：至少1个有效窗口即可进入训练池。旧V1每条本来超过96，因此旧训练未因这项门槛丢数据。旧V1验证每条独立抽96个互异窗口、合计288；短验证轨迹已采用min(96,N)个互异起点，不能复制凑数。本轮追加旧纠正的固定96点，合计384点，源级结果分开报告。真实逐帧例子见[窗口讲解](DataPrehandle/reports/20261020-window-walkthrough.md)。

## 3. 图像处理与训练随机增强

本节三图描述保留旧版实际路径。下一版只对主图、右腕图采用以下处理，左腕改固定占位并屏蔽；增强规则与确定性几何沿用。

训练、验证、推理共用以下确定性路径：

```text
原始压缩图 → RGB解码
→ head固定ROI；两腕保留全图
→ 按比例BILINEAR缩放、居中黑色letterbox到224×224
→ 训练才执行下表随机增强
→ float32，像素 /127.5−1 → 模型
```

- head源图1280×720；归一化ROI `[0.24,0.04,0.96,1.0]`，实际整数框`[307,28,1229,720)`。图像分辨率/相机位姿变化须重审，不静默套错框。
- 当前ROI宽922、高692，确定性输出内容约224×168，上下各28像素黑边。左右腕同样等比缩放补边，不拉伸。
- π0.5库有基础预处理，但不会识别白桌ROI。保留当前 [process_image](src/f1_toys/processing.py) 的确定性算法，不能复制DM0.5的448分辨率。
- 2026-10-16 [ROI候选实图对照](DataPrehandle/visualizations/20261016-roi-comparison/README.md)：仅提高上界不改变物体像素尺度；再收右侧仅放大约5.9%，抬升/右移余量减少。当前建议保留原ROI；候选仅预览，未修改生产合同。

| 顺序 | 主图 | 左右腕图 |
|---|---|---|
| 1 随机裁剪 | 固定95%**面积**、正方形随机位置，resize回224；p=0.5 | 无 |
| 2 随机旋转 | 均匀±2°；p=0.5 | 无 |
| 3 ColorJitter | brightness0.3、contrast0.4、saturation0.3、hue0；p=0.5 | 同左 |
| 验证/推理 | 不做上述随机增强 | 不做上述随机增强 |

参数及每操作独立概率继承 [DM0.5/F1图像recipe](../../F1/foam_sort_whole_body/docs/training/dm05_eef_delta_recipe.md)；插值、边界填充和裁剪取整以冻结的真实增强实现为准，写入配置/预览参数，不能只记录“±2°”。无翻转、无额外大幅遮挡；几何增强作为温和观测扰动，不变换机器人动作坐标。

**增强只能有一个所有者：任务训练adapter。** 关闭core内部二次随机增强。本轮由 [augmentation.py](src/f1_toys/augmentation.py) 实际执行，core内部增强关闭。生产处理已与用户审阅的9张确定性图、27张增强图逐像素一致。

每次训练读取由NumPy RNG产生新的draw seed，再按相机/操作独立派生；不同读取draw应有变化，断点恢复能继续正确序列，多卡不重复同一随机流。预览固定seed并保存replay参数；正式数据不离线永久烘焙随机增强。

历史ROI抽查为5条录制/79时刻，不能替代随机裁切后的覆盖检查。新增 [真实图像预览](DataPrehandle/visualizations/20261016-recipe-preview/README.md) 展示当前确定性输入与候选增强输入；生产处理核验见[单条图像验收](training/experiments/20261016-v1-eef-lora.md)；以后相机位置、ROI或增强参数变化须重新审图。

## 4. EEF标签与原生运动学


对每个观测起点t，使用右臂**反馈**和同一原生运动学模型：

```text
T_anchor = FK(q_feedback[t])
T_target[k] = FK(q_submitted_on_30hz_grid[t+k])
DeltaT[k] = inverse(T_anchor) @ T_target[k], k=0..L-1, 1 <= L <= 50
label[k] = [DeltaT平移3, DeltaT旋转矩阵前两行6, submitted右夹爪]
```

平移单位m，在当前末端轴系表达；有效前缀的每一步均相对同一个锚点，不能逐步累积。rotation6D解码采用与旧F1 reader一致的正交化，明确拒绝非有限/退化结果，不能悄悄填单位旋转。

FK锚点始终使用**原始精度的物理state16**，绝不使用已经归一化、clip或token量化的状态；量化仅用于模型观测条件。

**沿用既有 F1 原生DH/FK＋受限连续IK，不再比较选型。** [ArmModel/ContinuousIK](../../F1/foam_sort_whole_body/robot/kinematics.py) 包含第8行末端固定变换；HAL度数输入、内部rad、位姿m。F1-09 [原生FK fixture](../../F1-4242/tests/fixtures/f1_native_fk.json) 与该模型绑定的DH配置SHA一致。

已有 [同批数值验证](../../F1/foam_sort_whole_body/results/continuous_ik_20260916/README.md)：FK对SDK60,806条请求最大位置差3.91e-7mm；连续IK19,900次通过。前20步右臂直接SDK限位失败2134/3980，受限适配0/3980。后续 [64条扩测](../../F1/foam_sort_whole_body/results/ik_coverage64_20260916/README.md) 中右臂73,300次无拒绝，但有局部关节变化放大；不能声称所有预测都可解或连续。

这里的IK实际为受约束连续求解，不是把SDK返回分支直接下发。当前未找到同模型/同数据的Pink对照正式报告；[URDF与native模型差异](../../F1-4242/docs/MODEL_AUDIT.md) 不等于求解器胜负实验。标签FK与输出IK必须绑定同一native模型，不能混用尚有几何差异的URDF。

每个新数据/模型版本的接线验收包括：新V1首/中/末及抓取/插入起点的`joint→FK→相对EEF→解码→右IK→FK`；首步以当前反馈为参考，其后以前一接受解为参考。目标关节真值只用于评估，不作为求解种子。冻结模型/配置/限位/端点SHA。

沿用严格数值基线0.01mm/0.001°，这不是机械实测精度。泡沫的5mm/0.05°演示容差不迁入插孔任务。预测目标不可解则如实失败，不改标签、不放宽门槛、不裁关节后冒充原EEF解。

## 5. 旧版归一化与动作/时间mask

本节左臂状态归一化和左夹爪编码仅适用于旧版。下一版依第0节加入独立的状态信息隔离与相机mask；动作维mask和时间mask仍保持各自职责，不能相互替代。

状态和动作分开处理。旧smoke用mean/std归一化状态导致大量数值落到tokenizer范围之外，说明“编码与官方一致”不等于输入分布合适；本配方保留分字段规则，旧诊断与完整推导见[整理前历史](training/experiments/20261021-training-recipe-history.md#5-分字段归一化与mask)。

| 字段 | 当前处理 |
|---|---|
| 右臂状态关节 | 有效训练观测去重后取min/max，映射[-1,1] |
| 左臂状态关节 | 冻结控制器HAL合法范围；不放大静止噪声，不借用URDF/rad范围 |
| 近常量状态 | span小于1e-4时使用1°跨度、以原中心归一化；统计记录索引和规则 |
| 状态夹爪 | `(g−50)/50`，固定0–100量程 |
| 状态越界 | 离散编码前限制范围，同时记录原始物理偏差与clip索引；不把饱和当正常 |
| 动作相对xyz | train-only mean/std；std下限1e-6m，保存统计基数并可逆 |
| 动作rotation6D | identity；不对各旋转分量再做std缩放 |
| 动作夹爪 | `(g−50)/50`，反变换`50x+50`；仅转换一次 |
| padding | 存储/归一化零，不纳入统计或监督 |

动作不因状态token范围而硬clip。左侧未监督输出不进入控制：外部左侧沿Control最近有效submitted保持，不能用零硬件目标代替mask。统计实现见[prepare_formal.py](scripts/prepare_formal.py)，编码与反变换见[eef.py](src/f1_toys/eef.py)。新数据版本必须重算训练统计，不能借用单条旧范围。

动作维mask屏蔽后22维的带噪输入、初始采样噪声、更新与loss；时间mask则屏蔽每条样本不足50步的尾部。两者职责独立，bool mask不充当任意loss权重。[reader](src/f1_toys/dataset.py)核对非空连续前缀、metadata及合同；模型按每起点有效长度归一化，保持不同microbatch/梯度累积下的权重一致。

## 6. Full训练、评估与发布

本次Full更新视觉、语言和动作相关base参数，关闭原生LoRA，保留有效维mask和共享处理器。实际优化器、学习率、global128组成、保存/评估间隔及耗时集中在[V1_FullSFT](V1_FullSFT.md)，不在本页重复另一份参数表。训练时RTC仍暂缓；推理时RTC不改变当前监督损失。

新版本依次验收：真实输入/标签与反变换 → 有限loss/梯度及正确可训练范围 → 完整保存恢复 → Full权重导出逐值检查 → 固定观测生成评估 → 现场完整任务。恢复必须核对优化器、scheduler、RNG、数据游标与增强流，不能仅证明能加载权重；Full导出不套用LoRA“冻结张量应保持不变”的断言。

评估分别看抓稳、运输、对孔、插入释放、撤离/回位及首次拒绝，区分开环误差、IK通过与任务成功。短窗只统计前min(20,L)及有效L，仅L=50报告完整H50。扩大抓取开合和Home阶段覆盖的要求与本轮尚未覆盖的部分见[1K复盘](training/experiments/20261020-v1-fullsft.md#评估覆盖不足不能据此宣称学会抓取释放和回位)；不能把“要求评估”写成“已完成”。

发布bundle绑定权重/base身份、Full训练范围、状态与动作统计、ROI/增强策略、prompt、mask、FPS/H、运动学/限位和代码依赖身份。原始记录、旧数据包和运行release不覆盖；EEF checkpoint不可交给旧关节decoder。精确7关节Home和连续下一块选择仍属于当前示教未充分覆盖的范围。

每次训练包括smoke、预检和恢复均接W&B，正式与工程检查分run，恢复保持同run的step连续。历史V1 Full组为`03_V1_FullSFT`，当前单臂DAgger为`04_RightOnly_FullSFT`，阶段采样为`05_DAgger_StageSampling`，学习链路诊断为`06_LearningChain_Diagnostics`，工程检查为`00_Engineering_Checks`；完整分类和run入口见[Full实验报告](training/experiments/20261020-v1-fullsft.md#wb分类)。网络不可用时明确offline并保留同步步骤，不用TensorBoard代替W&B，不编造回填指标。

## 历史与复用依据（需要追溯时再读）

- [本页整理前完整历史](training/experiments/20261021-training-recipe-history.md)：单条LoRA配方、15条旧V1、50K续训及八卡→四卡推导；其中“当前”均为历史措辞。
- [单条EEF/LoRA验收](training/experiments/20261016-v1-eef-lora.md)、[15条LoRA与50K记录](training/experiments/20261016-v1-formal-training.md)、[四卡恢复](training/experiments/20261019-four-gpu-resume.md)、[50K曲线与Full决策](training/experiments/20261020-v1-50k-review-full-plan.md)。
- 旧教训：[DM0.5归一化/增强/EEF](../../F1/foam_sort_whole_body/docs/training/dm05_eef_delta_recipe.md)、[global batch不一致](../../DM0.5/docs/training/incidents/2026-07-28_04_GLOBAL_BATCH_MISMATCH.md)、[恢复dtype变化](../../DM0.5/docs/training/incidents/2026-07-28_08_RESUME_ADAPTER_DTYPE_CHANGE.md)、[生成轨迹退化诊断](../../F1/foam_sort_whole_body/docs/training_plan.md)。
