---
updated: 2026-09-17
type: training_recipe
status: user_confirmed_protocol_pending_formal_data
---

# DM0.5验证与checkpoint选择

本文细化[训练recipe](dm05_eef_delta_recipe.md)的验证部分。2026-09-17用户确认100K单run、沿用NewLoRA两阶段验证，以及双臂80%／夹爪10%／底盘10%的候选排序权重。**训练loss保持原配方，各有效元素等权；该比例只用于选checkpoint。** 正式来源与完成度见[当前方案](../training_plan.md)。

**09-17部分底盘监督补充：** 对v2合同，生成指标必须使用逐帧action_mask；unknown不属于“静止”，不计底盘MAE/RMSE，差分指标要求差分涉及的所有帧均有标签。当前底盘缺失时hold-current基线为空。报告保留known/unknown帧数与available计数；缺少底盘指标不能按0误差排分。双臂指标和已确认的80/10/10排序方案不变。

生成过程只屏蔽32维中的9维padding，全部23个物理维度均正常生成；不能把未来标签缺失mask传给采样器。原生采样器会在每一步将此mask乘到动作噪声，泄漏离线标注的可用性并改变生成轨迹。逐帧监督mask仅用于训练/held-out FM及生成后的误差计算。启动前已修正此接线并检查生成mask与监督mask相互独立，见[正式启动记录](../../results/formal_training_20260917/README.md)。

## 为什么保留两阶段、调整评价字段

本地[NewLoRA实测报告](../../../../DM0.5/docs/training/experiments/new_lora/VALIDATION_REPORT.md)中，held-out FM在35K最低，生成动作筛查在75K的MAE点估计最好；密集比较中70K/75K优势分散在不同分项，最后选择75K主选、70K备用。这支持保留生成动作比较、固定窗口和多seed，而不按FM最低值或最后一步自动选点。它不证明F1也会在同一步最好。

迁移依据是本地[固定窗口与生成评估](../../../../DM0.5/g2_sort/validation/evaluate_NewLora_actions.py)、[FM评估](../../../../DM0.5/g2_sort/validation/evaluate_NewLora_fm.py)、[Pass B配对比较](../../../../DM0.5/g2_sort/validation/summarize_NewLora_passB.py)。F1重新实现任务适配，去掉G2的34D字段、80条/15个checkpoint硬编码和量纲混合总MAE；F1采用MeanStd的字段也不能用`abs(normalized)>1`当作分位越界。

### 迁移范围：复用DM0.5，不重建通用框架

2026-09-17用户再次明确只迁移已打通的DM0.5方案并做demo适配。对应关系如下：

| 既有DM0.5部分 | F1处理 |
|---|---|
| `opendm/exp/dm05_exp.py`、`trainer/trainer.py`、`model/dm05/dm05_lora.py` | 直接引用：模型、LoRA、FM、optimizer/scheduler、checkpoint读写及恢复；不另写训练器核心 |
| Trainer的W&B集成 | 直接复用；F1仅设置项目/run身份并展开任务指标 |
| `build_episode_balanced_manifest.py` | 迁移同一等分中点采样，入口换成F1有效H50起点列表；去掉80条假设 |
| `evaluate_NewLora_fm.py`、`evaluate_NewLora_actions.py` | 保留固定窗口/多seed/10步Euler；适配state19、EEF23/pad32、字段归一化及冻结IK |
| `summarize_NewLora_passB.py` | 保留episode配对bootstrap；替换G2字段与65/70/75K固定列表，增加已确认80/10/10排名 |
| `infra/host/run_NewLora_validation_4gpu.sh` | 沿用逐checkpoint独立进程、保存结果后跳过的批处理方式；F1入口接受显式清单，核对release/权重身份，不硬编码GPU4–7或覆盖原容器 |
| `select_final_checkpoint.py`及NewLoRA实测选择报告 | 保留主选/备用及理由留档；旧脚本按G2全关节MAE自动排序的规则，按已批准F1流程替换 |
| `g2_sort/validation/nccl_smoke.py` | 直接运行原脚本检查实际空闲GPU通信，不另写NCCL测试 |

F1新增的轨迹复核图只服务于后补底盘与夹爪协同；报告身份校验、原子写入、长验证等待属于本次接线的运行支持，不改变训练recipe。工程检查脚本只证明接口迁移，不增加正式训练run。逐项证据见[本轮迁移收口](../../results/validation_infra_20260917/README.md)。

## 两阶段协议

| 阶段 | 范围 | 每条验证录制的生成评估 | held-out FM |
|---|---|---|---|
| Pass A | 每5K一次，5K至100K，共20个保存点 | 4个固定窗口×2个seed | 16窗口×3个独立seed |
| Pass B | 训练后从A筛出2–3个候选 | 16个固定窗口×5个seed | 16窗口×3个独立seed |

两阶段均覆盖正式release中全部入选validation录制。按每条录制有效H50起点列表等分取中点，短来源取全部可用起点，不重复凑数、不跨无效区间；固定无随机图像增强、10步Euler生成、评估batch1。具体seed写入[experiment.yaml](../../models/DM0.5/experiment.yaml)。每窗口随机种子由seed、episode、start和flow/generation流分别派生，FM随机消耗不会改变生成噪声。

先汇总每条录制，再按录制等权计算mean、median、P90和worst。比较要求来源、统计、窗口、seed、评估代码和运行环境合同一致；Pass A和B不同窗口的分数不能直接相减。配对bootstrap以episode为重采样单位，20,000次、seed20260821；区间包含0不证明等价，也不表示闭环任务成功。极少验证来源时区间解释力有限。

## 80/10/10如何计算

本demo的底盘标签含重建系统误差，双臂/夹爪来自记录。因此上肢主导选点，底盘拟合误差作为辅助。**80/10/10是用户确认的工程优先级，不是论文最优比例，也不是统计置信概率。** 给底盘更小权重不能修正其系统误差。

采用实际执行前20步H20的episode等权均值：

| 组 | 内部指标 | 总权重 |
|---|---|---|
| 双臂 | 左/右EEF位置距离mm、左/右旋转测地角度°，四项各20% | 80% |
| 夹爪 | 左/右目标MAE，各5% | 10% |
| 底盘 | vx/vy/wz MAE，各10%/3；只取H20的k=1..19 | 10% |

底盘排除k=0是因为当前速度已在输入中，沿主recipe的“单独评价未来速度”约定。完整H20/H50及k=1..49底盘结果仍报告，不改变训练chunk索引。

在同一冻结候选池内，每项误差从小到大排位，并列取平均名次；转成`(名次−1)/(候选数−1)`，再按上述权重相加，越小越靠前。常量/完全并列指标不给任何候选优势，不临时重分权重。记录完整候选池，增减候选后必须重算，不能跨池比较分数。排名避免把mm、度、m/s硬相加，但丢失差值大小，必须同时看原始物理误差和配对区间。

缺失窗口指标、非有限输出或无法解码的旋转标记为需复核，不通过只平均剩余有效项获得有利分数；这不是新增IK拒绝率门槛。IK失败率单独报告，保留已接受的demo基线缺口。

## 底盘权重低，不等于可以忽略协同

用户随后指出上肢合计90%可能掩盖底盘问题。这里区分：

- 速度幅值与后补标签的偏差：降低其决定checkpoint的影响，不能据此断言真实速度更差。
- 方向、起停、抓放配合：单独检查原始预测轨迹和对应窗口画面；明显问题不能被上肢综合分数覆盖。
- 候选底盘表现相近时，优先看双臂与夹爪；存在明显冲突时记录理由后确定主选、备用，不自动选择最高排名。

目前代码提供运动/停车分项、未来速度误差、保持当前速度/零速度基线及原始预测；**尚无经确认的事件阈值和任务阶段标注，因此不宣称已自动评价“放料前停稳”或夹爪转换时差。** 这些需结合轨迹复核，不擅自增加停车、IK或速度硬阈值，也不自动否决正式数据。

## 其他诊断与最终选择

报告H20/H50左右EEF位置/角度、夹爪与底盘误差、h1/5/10/20/25/50点误差、位置/速度变化诊断、seed间差异；冻结IK只解前20步，记录拒绝原因、首步关节跳变、连续变化和导数。导数不是新控制限值，成功IK子集的连续性需结合拒绝数阅读。

FM为辅助诊断；W&B记录训练曲线和每5K各组验证误差、IK拒绝率。逐窗口预测/真值、原始物理分项和配对比较本地保留。Pass A筛候选，Pass B密集比较并复核协同后，记录主选与备用理由，发布时使用一个checkpoint；不按总排名自动早停或自动加载best。

## 实现入口

[任务适配说明](../../models/DM0.5/f1_training/README.md)；配置及数据冻结后，在既有DM0.5环境运行：

```bash
python -m f1_training.validation manifest --release RELEASE.json --stage pass_a --bound-release BOUND_RELEASE.json --output pass_a.json
python -m f1_training.validation manifest --release BOUND_RELEASE.json --stage pass_b --output pass_b.json
python -m f1_training.validation evaluate --release BOUND_RELEASE.json --checkpoint CHECKPOINT_DIR --selection pass_b.json --output EVAL_DIR --device cuda:0
python -m f1_training.validation compare --reports REPORT_A.json REPORT_B.json --bootstrap-repeats 20000 --seed 20260821 --output comparison.json
```

这里是参数模板。正式训练使用新生成的`BOUND_RELEASE.json`，绑定与Pass A完全相同的`evaluation`；`--bound-release`不改变任何审批标记、标签、split或统计，也不覆盖原release。工程候选需要显式`--engineering`，不能据此启动正式训练。比较器保留评估合同，独立加载与训练回调若存在PEFT合并/运行环境差异，需要在同一条件下重评全部候选。最初迁移范围见[原记录](../../results/validation_migration_20260917/README.md)，本轮补充见[收口记录](../../results/validation_infra_20260917/README.md)。

### 批量评估、复核与留档

在6000D既有DM0.5容器终端，项目按主机绝对路径挂载、工作目录为`/home/xza_100850113/projects`：

```bash
export PYTHONPATH=/home/xza_100850113/projects/F1/foam_sort_whole_body/models/DM0.5:/home/xza_100850113/projects/DM0.5/opendm
export MPLCONFIGDIR=/tmp/f1-matplotlib
# 实际路径由正式run填写；这里不占用GPU或启动任何任务。
python -m f1_training.validation sweep --release BOUND_RELEASE.json --selection pass_b.json --checkpoints CHECKPOINT_A CHECKPOINT_B --output SWEEP_DIR --device cuda:0
python -m f1_training.validation compare --reports REPORT_A.json REPORT_B.json --bootstrap-repeats 20000 --seed 20260821 --output comparison.json
python -m f1_training.validation review --report REPORT_A.json --output REVIEW_A --limit 12
python -m f1_training.validation decision --comparison comparison.json --review human_review.json --output final_decision.json
```

`sweep`接收明确候选列表，不自行选择GPU或候选；每个checkpoint独立子进程释放显存，日志保存在`SWEEP_DIR/NNN_checkpoint-N/attempt-NNN.log`，完整报告在同名attempt目录内。中断后原命令重跑：校验已完成报告/权重/原始预测的身份后跳过；失败attempt保留，新建下一attempt。没有report.json的半成品不当成功，合同变化须新目录；不覆盖旧结果。

`review`生成逐窗口CSV、SVG和Markdown。默认12窗口仅是查看数量，**不改变验证采样或排名**；优先显示IK拒绝和手臂大误差窗口。要全面复核底盘/夹爪，应增大`--limit`至全部窗口并结合录制画面，不能只看前12张就宣布协同通过。图中标出实际前20步、夹爪目标/预测、底盘重建标签/预测及当前速度输入。

`human_review.json`包含`primary`/`fallback`的报告绝对路径、`selection_reason`、`base_coordination_review`、`ik_and_invalid_output_review`，各理由由复核者填写。`decision`验证2–3份Pass B报告及SHA后保存主选/备用的checkpoint身份与人工理由；不改软链接、不部署、不代替现场验收。

每5K训练内验证通过单独CPU通信组让其余rank等待，避免长验证在训练NCCL组上挂着等待操作；24小时是进程等待上限，不是训练或评估预算。异常广播至所有rank。W&B沿共享Trainer上报分项与episode mean/median/P90/worst、可用数及验证耗时，本地保存完整轨迹；在线账户与上传仍在正式启动时验收。
