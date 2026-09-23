---
created: 2026-09-15
updated: 2026-09-17
type: training_recipe
status: implementation_in_progress
recipe_id: F1_foam_dm05_lora_eef_relative_rot6d_v1
design_revision: 2
observation_contract: state19_base_velocity_20260916
backend: DM0.5/opendm
progress_source: ../training_plan.md
---

# F1 泡沫任务：DM0.5 EEF delta 单版训练配方

**定案：三路图像＋任务文本＋19D状态（双臂14＋夹爪2＋底盘当前速度3），预测23D实际动作，补齐到DM0.5原生32D；采用同一观测锚点的EEF相对轨迹、连续旋转6D编码、LoRA与原有flow matching。body5不进入模型输入。正式训练一个run，从中选择一个checkpoint发布。**

本文记录2026-09-15形成、2026-09-16修订输入合同后用户认可的训练决定、推导及来源。它是编码依据；[任务配置与接线实现](../../models/DM0.5/f1_training/README.md)已开始落地，工程验证不等于正式训练验收。数据清洗进度、候选范围和完成状态仍只维护在[当前方案](../training_plan.md)。

2026-09-15用户将FK/IK往返、正式数据reader/replay、执行时序三个TODO留待后续；2026-09-16落实19D输入，并按用户后续授权开始运动学验证与reader并行移植。完成度和验证结论见[当前方案](../training_plan.md)，完整训练配置与正式数据尚未发布。

## 1. 前因：这次从什么基础迁移

| 阶段 | 已有实现或当时选择 | 与本版的关系 |
|---|---|---|
| G2第一版LoRA | state/action各34D；双臂14＋灵巧手20；绝对关节目标；图像补边到正方形并缩放448 | 不是EEF；第一版也已有基础图像预处理 |
| G2第二版NewLoRA | 双臂14维改为相对当前反馈的joint delta，手指20维仍绝对；增加crop/rotate/color训练增强；global batch32、75K步 | 可复用训练机制、优化器和图像管线；动作字段、统计与协议不直接沿用 |
| F1原数据/回放设计 | state21；19D硬件目标＝双臂关节14＋夹爪2＋底盘速度3 | 保留为数据来源与关节回放中间表示，不原地改写清洗包 |
| 本次正式训练决定 | state19包含当前底盘速度、移除body5；固定EEF delta；模型结构不变；只训练一版；保留右臂与右夹爪；任务包含返程、排除随后双臂复位 | 从源state21和已复核底盘轨迹构造state19；将有效19D关节动作经FK和新编解码转换成23D策略动作 |

本地依据：[G2第一版配置](../../../../DM0.5/g2_sort/training/dm05_lora.py)、[第二版配置](../../../../DM0.5/g2_sort/training/NewLora_dm05_lora.py)、[F1原始接口合同](../action_interface_contract.md)。本版不延续G2 adapter及优化器状态，从已有DM0.5 base新建F1 adapter。

证据分为四类：**用户约束**是本次范围；**本地依据**是已读代码/已完成实验；**文献依据**是论文实验或官方实现；**工程选择**是将这些依据用于F1的具体取值。工程选择不标成论文证明的F1最优值，论文成功率也不作为F1预期成功率。

## 2. 输入19D：包含当前底盘速度，移除body5

| 状态索引，左闭右开 | 内容 | 来源 |
|---|---|---|
| `[0:7]` | 左臂7关节反馈 | `/hal/joint_states`，按名字取值 |
| `[7:14]` | 右臂7关节反馈 | `/hal/joint_states`，按名字取值 |
| `[14:16]` | 左右夹爪反馈 | F1夹爪反馈，0–100 |
| `[16:19]` | 当前底盘`vx、vy、wz`，m/s、m/s、rad/s | 训练：已复核后补轨迹在当前`t`的速度估计；推理：`/move/State.speed`反馈 |

### 2026-09-17缺失底盘补充合同

正式demo采用`f1_state19_eef23_partial_base_v2`：仍为19个状态槽、23维动作补至32维，同一模型联合训练。双臂/夹爪的原有效H50窗口不因后补底盘未知而丢弃。未来未知底盘仅`action_mask[:,20:23]=false`；当前未知底盘在`state_mask[16:19]`标记，task tokenizer输出三个`unknown`文本字段。容器中的0仅占位，不能经过旧tokenizer编码成实测停车。

使用task的`f1_data.state_tokens.make_tokenizer`，不要直接调用旧ChatTokenization处理本合同。统计只使用有效train值；动作MeanStd按各维有效mask累计，状态关节仍用train min/max。模型训练继续复用原生action_mask，不新增右臂持位降权或整臂屏蔽。生成评估也遵守mask：unknown不计为stopped、不参与底盘误差或跨缺口差分；当前速度未知时不计算hold-current基线。保留可评估数量，不能把缺失写成0误差。

验收与冻结状态只见[当前方案](../training_plan.md)，实现/证据见[部分底盘监督版本](../../results/partial_supervision_pool_20260917_v2/README.md)。以下完整底盘样本的物理含义保持不变。

### 2.1 后补速度同时用于当前状态与未来监督

用户已说明底盘移动方式固定，并确认路线距离，因此本版接受已复核后补速度作为完整训练样本中的底盘状态估计。**后补这一来源本身不构成排除输入的理由；当前底盘信息明确进入策略。** 训练与推理都给模型三个相同物理含义的量：当前车体速度。训练使用重建估计，推理使用现场反馈，不要求部署时重跑离线重建器。

同一条重建轨迹在`t`取值是当前状态，在`t+k`取值是动作chunk监督，详见§4.1。它们承担不同角色，但允许共享来源。本版把该轨迹也用作期望速度目标，是固定移动方式下的工程近似；没有据此声称逐时刻估计与实测完全相等。导出元数据保留`reconstructed_velocity_estimate`、重建版本和来源时间；这些审计字段不作为额外模型输入。

这种重建可利用整段轨迹的复核结果，提供的是对当前`t`的速度估计。图像、关节反馈仍按当前观测时间选择；未来速度序列只进入标签，不把未来端点、完整轨迹或人工阶段真值喂给模型。后续现场核对速度的坐标、符号、尺度与起停延迟，用于训练/推理一致性验收。

推理按`speed.linear.x、speed.linear.y、speed.angular.z`取值，沿用同一单位和归一化参数；底盘位置`x/y/yaw`不加入本版输入。协议依据：[底盘接口文档](../../../通用机器人软件接口文档.md)第3131–3143行。后补数据沿用已完成的[v3复核与合成结果](../../results/base_relabel_v3_autonomous/README.md)，不因新增状态字段重新要求录制或重做不变的数值核验。

2026-09-16两轮057现场回放提供了新的估计/反馈差异证据：任务可完成并不证明底盘瞬时速度一致。正式数据冻结前，按[F1清洗到recipe的检查表](../data_cleaning/f1_data_pipeline.md#按正式recipe审核每一批派生数据)评估当前速度输入的训练/推理差异；保持本节19D决定，不用放大后补标签或把命令冒充实测反馈来消除差异。数值与现场观察见[回放复盘](../incidents/replay_failures_and_lessons.md)。

### 2.2 为什么去掉body5

body5不是底盘三个量加腰部两个量。系统文档将其映射到UDP电机ID3..7，目标角度单位为度，并给出了j3..5参与站立的示例；每轴的机械含义、机构耦合和采集/部署数值约定仍须核对，不能自行命名成腰yaw、pitch、升降等。依据：[系统接口清单](../../../F1系统接口清单.md)第417–446、1224–1231行，[系统未闭合项](../../../F1系统配置.md)，[既有body字段审查](../archive/body_mapping_and_cycle_review.md)。

**body5不能测量或唯一确定底盘的vx、vy、wz。** 同一个站姿下，底盘可以停车、前进、横移或旋转。已有数据中body5几乎恒定，也没有表达移动路线的位移。

本任务按相容站姿执行，近常量body5提供的任务变化信息有限，因此从模型输入移除，保留双臂/夹爪16维并加入当前底盘3维，得到19D。此决定来自本任务范围与用户选择，不是文献证明所有任务都该删除身体反馈。状态走DM0.5文本token路径，少五个标量可缩短状态编码，但不据此承诺显著降低整模型计算成本。

body5继续保存在原始数据、来源包和部署姿态检查中。历史预检中body_j4曾相差约22.53°，说明执行仍须恢复相容姿态；不把身体反馈送入策略不等于身体姿态可以任意改变。[历史预检](../../results/demo_778_tail/f1_preflight_20260914/findings.md)

七关节手臂反馈包含肘部/冗余姿态信息，因此输入继续使用关节状态，输出使用EEF相对轨迹。输入与输出维数不要求相等。头部及body均不作为动作输出。

### 2.3 版本与迁移边界

| 日期 | 决定及依据 | 当前效力 |
|---|---|---|
| 2026-09-15 | 初版沿来源state21保留body5，后补底盘只作动作监督 | 输入部分被本次修订替代；文献、EEF表示与单run训练方案继续有效 |
| 2026-09-16 | 用户要求输入包含当前底盘信息；接受固定移动方式下的重建估计；移除低变化body5 | 正式采用state19，训练用当前重建速度，推理用当前速度反馈；F1效果待实际训练评估 |

原清洗/关节回放包仍是`state21/action19`。新reader读取源state的前16维，再拼接已复核底盘轨迹当前3维；由有效关节目标生成EEF动作23维。**新state19与旧action19只是长度相同，不能共用字段解释。** 源包中的body5及旧有效性证据保留，派生样本另存，不原地替换原始数组。

## 3. 输出23D：物理自由度与编码维数

F1每臂有7个机械关节；末端刚体位姿有位置3＋朝向3，共6个物理自由度。编码长度可以不同：

| 每臂位姿编码，不含夹爪 | 数组长度 | 双臂＋夹爪2＋底盘3 |
|---|---:|---:|
| 位置3＋欧拉角3或旋转向量3 | 6 | 17D |
| 位置3＋四元数4 | 7 | 19D |
| **位置3＋连续旋转编码6** | **9** | **23D，本版采用** |

“6D末端位姿”和“6D旋转编码”是两个概念；“7个关节”和“位置＋四元数共7个数”也不是同一个含义。原关节动作19D与四元数EEF动作19D即使形状相同，语义也不兼容。

连续旋转6D的依据是Zhou等对旋转表示连续性的研究[R1]，以及UMI直接采用相对位置＋rotation6D的机器人实现[R3]。欧拉角有奇异和分支跳变；四元数有正负双覆盖；旋转向量在π附近也有分支切割。50步/30Hz约覆盖1.67秒，整段相对同一锚点，不能预设所有旋转增量都足够小。

| 动作索引，左闭右开 | 内容 | 语义 |
|---|---|---|
| `[0:3]` | 左臂相对位置 | 当前左臂端点坐标中的位移，m |
| `[3:9]` | 左臂相对rotation6D | 无量纲旋转编码 |
| `[9:12]` | 右臂相对位置 | 当前右臂端点坐标中的位移，m |
| `[12:18]` | 右臂相对rotation6D | 无量纲旋转编码 |
| `[18:20]` | 左右夹爪目标 | 绝对0–100接口值，不作delta |
| `[20:23]` | 底盘vx、vy、wz | 车体速度，m/s、m/s、rad/s，不作delta |
| `[23:32]` | 模型补齐维 | 0，`action_mask=false` |

**实际动作23D、模型容器32D。** 原生投影尺寸保留，新动作含义由本次适配学习；补齐本身不意味着不同机器人语义自动对齐。现有[PadAction及状态编码](../../../../DM0.5/opendm/opendm/data/transforms.py)和[推理裁出有效维度](../../../../DM0.5/opendm/opendm/exp/dm05_exp.py)提供复用基础，F1设`output_action_dim=23`。

## 4. EEF delta与执行合同

### 4.1 整段共享当前观测锚点

对每个手臂，在统一的数据时间轴上定义：

\[
T_a=FK(q_{feedback}[t]),\qquad T_k=FK(q_{command}[t+k])
\]

\[
\Delta T_k=T_a^{-1}T_k,\qquad k=0,\ldots,49
\]

模型预测`ΔT_k`的位置3和rotation6D。推理用请求中同一份反馈计算锚点，还原`T_target[k] = T_a · ΔT_pred[k]`。

- 锚点来自反馈，目标来自有效`/lead`硬件命令；不能把未来HAL反馈默认替换成示教目标。
- 首步索引采用已有chunk的`t`，不是自动改成`t+1`；记录时间对齐不等于物理延迟已校准。
- 50步始终共享请求锚点，不逐步累加，也不在执行每一步时重新相加。
- 使用完整相对刚体变换，位置表达在当前端点轴下；不能与臂基座轴下直接位置相减混用。
- 左右臂分别使用原生FK的参考系；body保持相容姿态。底盘速度另行输出，不构造合成世界坐标来解释手臂动作。

UMI官方将这种表示称为`relative`，与相邻步`delta`以及其源码标注的旧`rel`实现区分。[相对变换实现][R4]。本文标题中的“EEF delta”指上述当前锚点相对轨迹。

UMI reader在所核对版本中的默认参数仍是`rel`，不能直接复制默认值来实现本文公式；本版显式选用`relative`。UMI的100%/80%结果支持整段共享锚点，不能单独归因于本版这一具体坐标乘法约定。

底盘沿用同一30Hz时间轴。记复核后的速度轨迹为`v_hat`，则`state_base(t)=v_hat(t)`，`target_base[k]=v_hat(t+k)`，`k=0..49`。首项因此与当前输入相同，这是当前chunk索引与重建来源共同决定的，不擅自平移到`t+1`。验证另报`k=1..49`的底盘误差及起停转换，并与“保持当前速度”的简单基线比较；首项复现不能代替未来运动预测效果。该基线仅用于评估，不增加训练run。

### 4.2 旋转解码与IK

**2026-09-16 demo取舍已定：** 用户接受现有实现为首版基线，版本固定为`f1_ikfk_demo_v1`，完整参数与快照见[IK/FK demo recipe](ik_fk_demo_recipe.md)。暂缓多分支优化及进一步IK扩测，继续DM0.5离线接线；实测失败与现场待办保留。

固定rotation6D约定为旋转矩阵**前两行**展开；最终输出经Gram–Schmidt正交化恢复三行旋转矩阵，与[UMI位姿工具](https://github.com/real-stanford/universal_manipulation_interface/blob/d095ba9590df789df5189eea5ee7e431689038a6/umi/common/pose_util.py)及PyTorch3D[R5]的约定一致。仅对最终动作解码，flow matching中间带噪量保持原模型计算，不逐步投影到旋转空间。全零或近共线向量要识别为解码失败，不能把退化矩阵提交给IK。

本版端点采用原生FK配置定义；不把XR控制器/`/lead/*tcp`当真实机器人TCP。此前只读检查定位了F1已安装FK/IK服务，接口关节使用度、位置使用mm，支持4×4矩阵目标；当使用`target_pose`矩阵时，`target_xyzabc`应为空，避免其优先覆盖矩阵。训练内部m与接口mm在适配边界转换。

当时读取的工具偏移配置为零，不能把配置文件值直接当已核实的运行参数。开写适配时须固定实际端点及工具变换，绑定合同版本。系统文档中部分旧示例字段有差异，以实际安装的srv及后续版本化证据为准。[系统接口](../../../F1系统接口清单.md)

**采用与原生FK核对一致的改进DH模型＋受限数值IK适配。** [实现与验证依据](../../results/continuous_ik_20260916/README.md)：先在末端位姿约束下局部最小化关节解与参考的距离；遇肘部贴限位时采用受约束残差求解，独立检查末端误差和限位。矩形范围采用已核实控制器映射，另保留SDK的J6/J7耦合条件；不事后clip解来掩盖EEF误差。保留“当前反馈起始、前一接受解作为下一参考”，但替换原生IK的选解算法；数学层不再依赖ROS服务调用，原生SDK继续作为数值对照。当前不需要先引入Pinocchio/Pink才能移植。

七关节到6自由度EEF存在冗余，**固定的是EEF＋参考关节＋求解配置的选解规则，不是EEF单独唯一恢复原示教7关节**。原8条通过后，[新增64条扩测](../../results/ik_coverage64_20260916/README.md)暴露了逐步近邻选解的肘部分支及贴限位跳变；本次demo接受这些已知缺口，基于EEF chunk后续可达性与连续性的多候选选择暂缓。采用demo基线不等于整体验收通过。实机合理姿态、碰撞、首步过渡、速度/加速度与ARM耗时仍须分开验收。`solve_eef_prefix()`使用固定观测锚点还原action23，参考可取执行前较新的关节反馈；失败不输出整段关节目标。

2026-09-16已完成首批`q→FK→相对编码→还原→IK→FK`数值检查与连续种子诊断，证据见[运动学验证](../../results/kinematics_validation_20260916/README.md)。自身目标关节作参考的往返通过，不代表部署可用，因为推理时不知道未来示教关节。原生IK的成功标志与超限标志必须分别处理；SDK和控制器限位也须同时核对。仅修改限位或固定参考的离线诊断仍未消除问题。原生IK是位姿到关节的转换环节，不能与整段规划器等同。

2026-09-16[回放执行映射](../../results/replay_runtime_fix_20260915/README.md)已明确逐关节限位截断。标签导出前，须将`q_command`具体采用的有效执行目标映射版本固定，并核对与FK/IK限位一致；不能训练未处理的超限EEF目标、执行时再无记录地改变它。原lead和映射后目标均保留，当前回放通过不替代EEF轨迹验收。

## 5. 后训练方法：采用什么、依据是什么

| 决定 | 原理及证据 | 本版取值与证据边界 |
|---|---|---|
| 当前底盘速度输入、移除body5 | 固定移动方式与相容站姿是用户给定的任务前提；底盘反馈协议提供在线同名物理量 | state19；离线采用复核重建估计，在线采用速度反馈；属于2026-09-16任务决策，无F1增益数字 |
| 当前锚点的整段相对轨迹 | 避免相邻步预测误差逐步累积；UMI杯子任务整段相对20/20＝100%，相邻delta16/20＝80% [R2] | 采用§4合同；这是该任务表示消融，不是F1增益预测 |
| 连续旋转6D | 回避低维全局表示的不连续性；理论[R1]及机器人实现[R3] | 每臂位置3＋旋转6；没有F1的6D/四元数对比结果 |
| 全线性层LoRA，视觉参与适配 | 新相机和抓取细节需要适配视觉；OpenVLA相应实验full FT69.7±7.2%，r32为68.2±7.5%，r64为68.2±7.8%，冻结视觉47.0±6.9% [R6] | 沿本地r32、alpha16、dropout0、all-linear；该表为较小SigLIP-only模型、两个Franka任务的33次评估，不能当作DM0.5参数最优证明 |
| 按视角保守增强 | 降低对照明和图像小偏移的依赖，保留腕部抓取视野；openpi非腕图做几何增强、腕图不做 [R7] | 头图沿G2温和增强，腕图只做颜色增强；没有独立可归因的成功率提升数字 |
| 分字段归一化 | 避免量纲和分布压缩决定优化难度；LingBot-VLA 2.0的MeanStd55.0%、MinMax47.5%、未裁剪Q01–Q99为47.4% [R8] | 位置/速度用MeanStd，旋转/夹爪分别处理；该消融针对relQpos，将其用于F1是迁移判断 |
| 控制采样起点 | 避免重复空等占满batch；openpi DROID按未来chunk是否大多idle过滤起点，并报告改善策略 [R9] | 源录制均匀→有效起点均匀；只排除已确认任务外空等，没有手填阶段“最优配比”或收益百分点 |
| 后段学习率下降 | OFT ALOHA用5e-4，50K后降到5e-5，作者报告损失下降、收敛改善 [R10] | 本地warmup＋cosine到0.1倍；不是照搬其L1目标或收敛阈值 |
| 用生成动作选择checkpoint | 本地G2的held-out FM在35K最好，完整生成MAE的75K点估计最好 | 同run验证解码动作，不按FM单独早停；[本地报告](../../../../DM0.5/docs/training/experiments/new_lora/VALIDATION_REPORT.md) |
| 部署时序一致 | UMI同一投掷checkpoint启用延迟匹配105/120＝87.5%，禁用69/120＝57.5% [R2] | 固定时间轴并核定实机延迟；本版初始同步执行并不等于已实现UMI延迟匹配 |

OpenVLA表中的训练参数占比约1.4%，是它的r32模型统计，不是当前DM0.5实际可训练参数比例。实际比例应由本地trainable summary核验。OFT整体成绩是多项变化的组合，也不能分摊为上述单项trick收益。

## 6. 归一化、mask和采样的确定规则

### 6.1 分字段归一化

| 字段 | 规则 |
|---|---|
| 输入双臂关节14维 | **2026-09-17用户确认：改用有效训练状态min/max**缩放，保留256档状态token；验证集不参与范围统计，真实常量显式居中。旧q01/q99会裁掉稀有但有效的姿态，已不作为本版双臂输入规则 |
| 输入底盘当前速度3维 | 单独对有效训练观测时刻统计q01/q99，缩放至[-1,1]后沿用256档编码；分位区间退化但实际非恒定时改用有效min/max，真实常量居中；策略及参数写入元数据，推理复用且越界截到编码边界 |
| 输入/输出夹爪 | 固定`(g−50)/50`；右夹爪100映射＋1，反变换为`50x＋50` |
| 动作EEF位置delta、底盘速度 | 按最终H50动作标签计算train有效元素的MeanStd，50步共用每维统计；不硬裁剪有效动作标签 |
| rotation6D | identity normalization；元素已在[-1,1]；不做经验分位裁剪 [R3] |
| 其他真实常量动作维 | center取常量，scale取1；依然是有效监督，不冻结运行输出 |
| padding | 0、mask=false，不进入统计 |

MeanStd对非恒定维使用标准差的数值下限，导出时检查稀疏维的尺度和归一化分布；极小方差的处理须落入统计元数据，不能静默把稀有运动抹掉。[openpi数值问题说明及变换][R11]。可逆仿射缩放本身不会破坏旋转，问题在于不可逆裁剪、过度放大和最终非法旋转解码。

本版夹爪固定量程规则**替代旧设计中对所有常量维统一居中0的笼统约定**；两者均可定义为可逆变换，但训练与推理必须选同一种。`q01=q99=0`不证明全数据为0，必须检查真实有效值。统计不能使用G2、验证集、无效字段或默认重复尾部。

输入速度的状态统计与输出速度的H50动作统计分开保存。两者表示同一物理量，但输入进入离散状态token，输出参与连续flow matching，不能直接把无界MeanStd结果送进现有256档状态编码器。状态量化本身有误差，验收记录裁剪比例和反量化误差；动作仿射归一化另验可逆往返。

### 6.2 Mask与loss

- mask表示“这个目标是否具有有效监督”，严格为bool/0–1；无效数值先填有限值，再屏蔽，避免NaN乘0仍为NaN。
- 时间有效性、字段有效性和补齐mask共同生效；复位全动作无监督，不把复位前正常返程/停车一并删除。
- 当前不跨无效区间的窗口规则继续沿用；片段外未来若补齐，必须mask=false，禁止复制非零底盘末速度作为真值。窗口筛选仍服从正式数据验收。
- state19的当前底盘3维必须有有效重建估计；缺失或未决不能填0冒充停车。未来动作mask与当前观测有效性分别判断，未来标签有效不自动补足当前状态；原有来源mask不因维数变化而自动放宽。
- 常量右夹爪及有效0速度mask=true；停下也是需要学习的动作。
- 沿用每个样本内有效元素平均、再做batch平均的flow-matching MSE；所有有效维loss权重为1，底盘弱标签按已认可范围参与监督。
- **不能用0.5/2之类的mask表达loss权重。** 当前模型也用mask乘带噪输入，混用会改变训练噪声。若未来调整权重，应另设loss权重路径；本版不做此变更。

本地适配点：[BuildActionChunk、Normalize、ActionRelative、PadAction](../../../../DM0.5/opendm/opendm/data/transforms.py)、[NormStatsCollator](../../../../DM0.5/opendm/opendm/data/collator.py)、[FM实现](../../../../DM0.5/opendm/opendm/model/dm05/dm05_arch.py)。默认尾部复制/全有效mask及丢失mask的统计器不满足本版合同；现有向量相减也不能处理state19/action23和旋转组合。

### 6.3 数据与图像

训练混合为100%本轮验收的F1数据。继承原录制分组，派生片段不跨train/val；先均匀选源录制，再均匀选其中有效起点。只有已审核明确的任务外人工空等才排除起点；不能用“底盘速度为0”代替空等判定。保留抓持、停车、放置稳定和返程，不删除时间步后拼成加速轨迹。阶段标签只用于数据审核/评估，不作为推理时拿不到的真值输入。

| 预处理 | 头图 | 左右腕图 |
|---|---|---|
| RGB、黑色补边到正方形、448×448 | 训练/验证/推理一致 | 训练/验证/推理一致 |
| 随机裁剪 | 95%面积，正方形，p=0.5 | 不做 |
| 随机旋转 | ±2°，p=0.5 | 不做 |
| ColorJitter | brightness0.3、contrast0.4、saturation0.3、hue0，p=0.5 | 同左 |
| 验证/推理 | 不做随机增强 | 不做随机增强 |

每路按自己的策略采样增强；头图各增强操作独立采样。参数来自本地[NewLoRA增强](../../../../DM0.5/opendm/opendm/data/augmentations.py)，腕图几何策略参考[R7]。openpi的95%是宽高各95%，本版是95%面积，不能写成完全复刻openpi。保留本地增强参数、按视角缩小作用范围是F1工程选择。

## 7. 正式run参数与选模

| 项目 | 本版值 | 来源/性质 |
|---|---|---|
| 初始化 | 已验证DM0.5 base，新F1 adapter、独立输出目录 | 防止加载G2动作语义或错误续训；本地依据 |
| 模型 | 原生32D，chunk50，三路图像，max token length1024 | 本地实现；不更新上游架构 |
| 状态 | 19D＝双臂14＋夹爪2＋当前底盘速度3；body5不进模型 | §2的2026-09-16修订；训练重建速度、推理反馈速度 |
| 任务文本 | 将泡沫棉从取料位置抓起，移动到放置位置放下，再返回取料侧。 | F1任务定义；训练/推理同文同模板 |
| LoRA | r32、alpha16、dropout0、bias=none、all-linear | 本地配置＋[R6]的适配证据 |
| 完整训练模块 | action_in_proj、action_out_proj、time_mlp_in/out、dm05_time_modulators | 本地LoRA的modules_to_save |
| 优化器 | AdamW；betas=(0.9,0.95)，eps=1e-8 | 本地NewLoRA继承配置 |
| LR与schedule | 峰值5e-4；warmup1000；cosine到5e-5 | 本地配置；后段衰减也有[R10]支持 |
| Weight decay / 梯度裁剪 | 1e-10 / max_grad_norm=1 | 保留本地参数；不是论文固定最优值 |
| Global batch | 32；4 GPU×每卡4×accumulation2 | 本地已运行配置 |
| 精度/内存 | bf16、TF32、现有gradient checkpointing；DDP，FSDP关闭 | 复用现有环境 |
| Attention | LLM SDPA、vision FlashAttention2、action SDPA | 本地6000D已验证组合，启动参数需显式固定 |
| Loss | 原flow matching及时间采样，各有效维等权 | 本地模型机制；不更换为OFT的L1头 |
| 训练预算 | 单run，100K步；每5K保存并验证，保留20个保存点 | 2026-09-17用户指定假期demo预算；不代表100K必然优于中途checkpoint |
| 复现 | seed42；保存optimizer/scheduler/RNG及采样配置 | 完整断点；防错误恢复 |
| 训练监控 | W&B在线项目`F1_foam_DM05`；训练曲线、硬件指标与每5K生成验证指标，同一run恢复 | 2026-09-17用户明确要求；不改变优化器、loss或选模标准 |
| EMA / 状态噪声 | EMA关闭；状态noise/dropout为0 | 沿本地已验证范围；openpi LoRA配置也关闭EMA [R12] |
| 数据动作频率 | 30Hz，H50 | 当前数据合同；不等同底层伺服频率 |

模型结构不变的含义是保留DM0.5的网络及flow matching机制。LoRA覆盖视觉/语言/动作线性层，前向权重会随适配改变；它不意味着“只训练最后动作头”。本地配置分别见[LoRA定义](../../../../DM0.5/opendm/opendm/model/dm05/dm05_lora.py)、[优化器与exp](../../../../DM0.5/opendm/opendm/exp/dm05_exp.py)、[Trainer](../../../../DM0.5/opendm/opendm/trainer/trainer.py)。

“只训练一版”指一个正式run；中途checkpoint用于同run选优，不额外训练17D/19D/23D动作编码或不同权重的对照版本；这里的动作编码19D与已定的输入state19无关。正式数据数量以冻结版本为准，32–64条管线验收小批不自动另开过渡模型。本版按用户决定从step0配置100K完整日程：warmup仍1000，之后cosine在100K降至5e-5；不是先跑75K再重启学习率追加25K。每5K一个保存点，共20个，保留供选模。数据质量较弱不构成“训练更久必然更好”的证据；不以假期长度承诺完成时间，也不采用FM损失单独早停。

### W&B监控与恢复

2026-09-17检查发现早先任务配置的`wandb_project`为空，W&B实际未启用；按用户要求现改为`F1_foam_DM05`，通过共享Trainer的W&B集成记录训练loss/FM loss、学习率、梯度范数和运行期间硬件指标。每5K生成验证额外上报按episode等权汇总的H20/H50位置/旋转/夹爪/底盘误差、停车/运动子集及IK拒绝率；逐窗口原始报告继续本地保留。无有效运动子集时不填0，旋转有效项均值与无效计数分开记录；跨checkpoint排序另按下述已确认协议进行。

正式启动使用现有账户凭据，启动前核对登录与网络；本轮没有创建线上run或验证线上上传。正式入口要求online模式，从独立输出目录派生稳定`WANDB_RUN_ID`；新run使用`resume=never`，显式checkpoint恢复使用`resume=must`，避免静默另开曲线。rank0记录recipe和数据/统计/代码绑定到W&B config，并将run ID/URL写入输出目录`wandb_run.json`。不上传模型权重；CPU/GPU工程检查关闭W&B，避免混入正式训练记录。换机恢复须保持输出目录身份或另行明确迁移映射。

### 验证与唯一发布权重

固定验证来源、窗口、无随机增强和生成seed；完整生成使用10步Euler。按episode汇总，再分别报告任务阶段、左右臂、夹爪、底盘运动和停车子集，重点报告实际执行前20步，同时保留H50结果：

- EEF位置误差（mm）、旋转角误差（°），不是只报告rotation6D逐元素误差；
- 夹爪目标误差、开合转换时刻；底盘vx/vy/wz误差、运动/停车表现；
- 旋转解码失败、IK失败、首步跳变、关节连续性/限位及生成seed波动；
- FM loss作为辅助，离线误差不写成闭环任务成功率。

2026-09-17用户确认沿用NewLoRA两阶段流程：每5K对全部入选验证录制做4窗口×2生成seed筛查，FM另用16窗口×3seed；训练后对2–3个候选做16窗口×5生成seed密集比较，选择主选及备用，不按FM自动早停。来源内固定有效起点中点采样、episode等权汇总及20,000次配对bootstrap，具体seed与参数已写入配置；实际窗口随正式数据release冻结。

用户确认候选排序采用**双臂80%、夹爪10%、底盘10%**：双臂左右及位置/旋转各半，夹爪左右各半，底盘三轴等权；先在同一冻结候选池对各物理指标排名，再加权，主看实际执行H20，保留H50。底盘未来误差排除输入中已有的k=0。原因是底盘标签存在重建系统误差，上肢监督更可信；这个比例是本demo的工程选择，不是统计置信概率。**训练loss不变**。低底盘权重不能掩盖明显方向/起停/抓放协同问题；原始轨迹、IK拒绝和无效输出单独复核，不设置未经确认的新硬阈值。排名只辅助筛选，不能自动发布最高分候选。完整计算、实现边界和本地依据见[验证recipe](dm05_validation_recipe.md)。

## 8. 推理初始配方与明确保留的边界

6000D运行模型服务，F1运行请求/执行客户端。初始方案为同步请求，10步Euler，预测50步、执行前20个30Hz间隔后重新观测；这约为0.667秒动作窗口，不包含推理等待，不等于已测得的闭环频率。

每个请求携带三路图像、19D状态、观测时间与合同版本；其中双臂/夹爪16维来自现场反馈，底盘3维来自同一观测时刻的`/move/State.speed`。保留各流时间戳与新鲜度，不用上一条模型速度命令冒充当前反馈。body5用于客户端姿态检查，不打包进模型状态；任务文本在两端一致。恢复末端目标必须使用该请求的锚点，随后由§4.2冻结的demo IK适配转关节；原生IK直接下发尚未通过§4.2验证。夹爪和底盘按各自接口发送。底盘速度需明确有效期，等待下次推理时不继续沿用过期非零命令；不得突发补发积压命令。底层控制频率和插值遵循F1实际接口验证结果。

10步Euler及执行20步是沿本地经验选择的初始参数，没有F1的5/10步或执行窗口对比。初始同步方案并未自动实现UMI的延迟匹配；真实时延和过期动作调度必须在TODO中闭合。这里不启动G2现有34D协议，F1协议和相对位姿还原需要任务适配。

本版任务是抓取、搬运、放置、返程；复位尾段在监督和评测范围外，不据此宣称自主收臂或连续循环。身体姿态和头部保持相容配置，右臂和右夹爪继续作为策略输出，不用运行时冻结掩盖模型误差。

## 9. 接下来的三个TODO与代码移植顺序

**已有运动学数值验证和首版reader，从现有结果继续实现。** 清洗可继续并行，reader开发不必等待全部候选收口；正式run使用哪批数据，以最终冻结和验收版本为准。少量训练来源的统计可能无法覆盖验证姿态，正式发布须重新核对状态裁剪与量化误差，不把工程小批stats直接用于正式部署。

| TODO | 应形成的可审查结果 |
|---|---|
| 1. FK/IK版本与接入 | 使用已冻结的`f1_ikfk_demo_v1`，保留往返/连续求解证据与已知缺口；完成模型输出反归一化、固定锚点、前20步IK与失败路径的离线接线，并核对F1依赖和耗时。多分支优化、全池IK扩测及Pink替代暂缓 |
| 2. 新reader、数据冻结与replay验收 | 源state21/action19派生为三路图像＋state19/action23：臂/夹爪当前反馈、底盘当前重建估计、未来H50动作；完成mask、原split、独立状态/动作stats及EEF编解码，用同一reader离线replay；冻结正式范围并完成既定现场replay后，启动一个正式run |
| 3. 底盘反馈一致性、执行频率与延迟核定 | 将已能读取的`/move/State.speed`接入19D请求，核对坐标、单位、符号、时间与新鲜度；分别测量观测、传输、推理、IK和控制时序，验证30Hz调度、速度到期与停车，确定初始推理窗口是否可用 |

推进顺序：TODO1沿冻结基线接线，暂不继续算法优化或扩大IK测试；同时用已有通过工程检查的小批接入TODO2，开展TODO3中的只读接口/时间核对；随后合并EEF导出、训练实际reader与推理适配，完成离线一致性、现场replay和端到端时序验收，最后冻结数据与配置并进行一次正式LoRA训练。小批只验证管线，不另开过渡模型；现场机器人动作由用户本地执行。最新通过数量、部署结果及现场剩余项只见[当前方案](../training_plan.md)，这里不重复维护进度。

后续代码增量为：F1 reader及FK标签导出、EEF编解码、mask/统计、采样器、验证接入、F1推理协议及执行适配。共享模型源码继续引用`DM0.5/opendm/`。正式发布绑定数据版本、source split、FK/工具合同、图像变换、归一化、模型base、adapter、代码版本和运行参数。

[experiment.yaml](../../models/DM0.5/experiment.yaml)已于2026-09-17迁移为本recipe确定的参数，旧版另存[历史配置](../../models/DM0.5/experiment_state21_joint19.archive.yaml)。[任务接线](../../models/DM0.5/f1_training/README.md)按`f1_state19_eef23_rot6d_rows_v1`绑定reader、统计和发布清单；[data_contract.draft.yaml](../../configs/data_contract.draft.yaml)继续保留来源state21/action19合同。尚未发布正式数据或修改其批准状态，也未启动训练；不能仅以代码接通宣称模型与实机验收完成。

## 10. 参考文献与复现定位

资料核对日期为2026-09-15。论文优先给版本化arXiv或作者原文；UMI代码固定为`d095ba9590df789df5189eea5ee7e431689038a6`，openpi固定为`215abfb217dbac7d5f1273282331b9b1866c0479`，OFT固定为`e4287e94541f459edc4feabc4e181f537cd569a8`。其余GitHub `main`入口在实际引入代码时再固定revision。正文效果数字只归属于相应实验，不归因于整个F1组合配方。

| 编号 | 原始来源 | 支持本文的范围 |
|---|---|---|
| [R1] | Zhou et al., *On the Continuity of Rotation Representations in Neural Networks*, CVPR 2019 | 低维旋转表示的连续性问题、连续6D表示 |
| [R2] | Chi et al., *Universal Manipulation Interface: In-The-Wild Robot Teaching Without In-The-Wild Robots*, arXiv:2402.10329v3，§III-B、Fig.6、§V-A/B | 当前锚点相对轨迹；杯子任务表示消融；动态投掷延迟匹配消融 |
| [R3] | UMI官方 `umi_dataset.py` | 相对动作、位置/rotation6D/夹爪布局及旋转identity normalizer |
| [R4] | UMI官方 `pose_repr_util.py` | `relative`与逐步`delta`、旧`rel`的区别 |
| [R5] | PyTorch3D官方 `rotation_conversions.py` | rotation6D前两行约定及Gram–Schmidt解码 |
| [R6] | Kim et al., *OpenVLA: An Open-Source Vision-Language-Action Model*, arXiv:2406.09246v3，§5.3/Table 1 | LoRA与视觉适配；特定小模型/任务上的对比，不是DM0.5直接实验 |
| [R7] | openpi官方 `models/model.py` | 区分腕图/非腕图的增强实践 |
| [R8] | Wu et al., *From Foundation to Application: Improving VLA Models in Practice*, arXiv:2607.06403v1，§6.1 | 四个GM-100真机任务、相对关节目标下的归一化消融；其Q01–Q99实际未裁剪 |
| [R9] | openpi官方DROID训练文档，Data Filtering | 按未来chunk空闲程度筛选采样起点，不是删除帧后压缩时间 |
| [R10] | OpenVLA-OFT官方 `ALOHA.md`，Best practices | 后段学习率下降的官方训练经验 |
| [R11] | openpi官方 `transforms.py`及[README故障排查](https://github.com/Physical-Intelligence/openpi#troubleshooting) | 可逆归一化实现、低方差/稀疏字段的数值问题 |
| [R12] | openpi官方 `training/config.py`中的LoRA配方 | EMA并非LoRA必加项，不以full FT的默认EMA替代本次选择 |

[R1]: https://zhouyisjtu.github.io/project_rotation/rotation.html
[R2]: https://arxiv.org/html/2402.10329v3
[R3]: https://github.com/real-stanford/universal_manipulation_interface/blob/d095ba9590df789df5189eea5ee7e431689038a6/diffusion_policy/dataset/umi_dataset.py
[R4]: https://github.com/real-stanford/universal_manipulation_interface/blob/d095ba9590df789df5189eea5ee7e431689038a6/diffusion_policy/common/pose_repr_util.py
[R5]: https://github.com/facebookresearch/pytorch3d/blob/main/pytorch3d/transforms/rotation_conversions.py
[R6]: https://arxiv.org/html/2406.09246v3#S5.SS3
[R7]: https://github.com/Physical-Intelligence/openpi/blob/215abfb217dbac7d5f1273282331b9b1866c0479/src/openpi/models/model.py
[R8]: https://arxiv.org/html/2607.06403v1#S6.SS1
[R9]: https://github.com/Physical-Intelligence/openpi/blob/215abfb217dbac7d5f1273282331b9b1866c0479/examples/droid/README_train.md#data-filtering
[R10]: https://github.com/moojink/openvla-oft/blob/e4287e94541f459edc4feabc4e181f537cd569a8/ALOHA.md
[R11]: https://github.com/Physical-Intelligence/openpi/blob/215abfb217dbac7d5f1273282331b9b1866c0479/src/openpi/transforms.py
[R12]: https://github.com/Physical-Intelligence/openpi/blob/215abfb217dbac7d5f1273282331b9b1866c0479/src/openpi/training/config.py

### 本地代码证据指纹

2026-09-15读取时，工作区Git HEAD为`1cafaa9af6f828dca778109b5c563769a53021a4`，Git根为`projects/`，**不是OpenDM上游独立commit**。工作区存在未提交改动，不能仅凭HEAD还原；以下为读取文件的SHA256，路径相对`projects/`。这些是本次决策依据的指纹，不是已生成的F1训练发布包。

| 文件 | SHA256 |
|---|---|
| `DM0.5/g2_sort/training/dm05_lora.py` | `f25f973ecd04c4b17adcb87af56ce806afddfbca050743fa52df21e58c3b8bbe` |
| `DM0.5/g2_sort/training/NewLora_dm05_lora.py` | `4ae3a8de8ccbb0d6d8479659f529e13f45353d445f97442d22b5290b7b46b8c5` |
| `DM0.5/opendm/opendm/model/dm05/dm05_lora.py` | `768c7bc7321528e2d1d8d9ba914f119d7a1c3b1304da7adfaab01d1e6070fdd5` |
| `DM0.5/opendm/opendm/model/dm05/dm05_arch.py` | `2f06044dd22b0393dea6c7816d41037365146df2028d003ba2d35bdf87a8a590` |
| `DM0.5/opendm/opendm/data/transforms.py` | `b59269919d27c301b35a06e0568af5683bdb80b589278541e688bcc18ef43896` |
| `DM0.5/opendm/opendm/data/collator.py` | `cfd7cc83ec0348f0a8b0176bda056e95b101b3296be33bbaa7106a9e61ef441c` |
| `DM0.5/opendm/opendm/data/augmentations.py` | `961e6ad52715ce04bba1920292e78f51922c7e63c67775466cf3f262a22a229c` |
| `DM0.5/opendm/opendm/exp/dm05_exp.py` | `97820c33483c2533b15f58f2df9f455490925e2b861b2a9781e2bc0d7b8a3b3b` |
| `DM0.5/opendm/opendm/trainer/trainer.py` | `bce2a4591ee0edae5633ad8483867704f3c0963981c0a0d58ed041eb70805996` |
