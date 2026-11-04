# 15条成功V1：八卡LoRA训练

用户已授权开始训练，保留原英文prompt。目标是在当前木块与基础场景建立可重复的抓取/插孔基线；形状泛化和语言选物实验已放入TODO，IK边界研究继续暂停。

正式包已完成12条训练/3条验证划分、训练集统计与CPU真实数据回读。八卡预检、恢复验收和正式训练的实时状态统一见[STATUS](../../STATUS.md)，不能把本文的执行计划当成已完成训练。

```mermaid
flowchart LR
  A[15条原件 / 时间索引] --> B[12训练 / 3验证]
  B --> C[train-only统计 / ROI / EEF检查]
  C --> D[八卡20步预检 / 恢复21步]
  D --> E[2000 / 4000 / 6000步]
  E --> F[独立验证 / 固定噪声生成]
```

## 冻结数据与配方

- 训练11,791窗口、11,343不同输入观测；验证2,965窗口、2,902不同输入观测。按原episode隔离，同物体同场景，不声称新物体泛化。
- 30Hz、H50、过去整组输入、实际已完成发布动作；Home/控制来源/无效发布/日志缺口边界保留，当前起点与未来动作独立判断，无尾部填充。
- 三图、ROI A `[.24,.04,.96,1]`、224等比补边、正式随机增强、右EEF10、原生FK/IK合同沿用。prompt与旧模型逐字符相同。
- prompt：`Use the right gripper to pick up the wooden block and insert it into the matching opening of the container.`
- 从官方π0.5 fp32 base重新初始化LoRA；r32/alpha16/dropout0，现有196叶子及动作/时间投影范围。
- 八张H20，micro2、累积1、global16；AdamW峰值1e-4，warmup300，cosine6000步至1e-5，`min_lr_rate=null`；每500步保存与验证。新配置为[正式YAML](../../configs/sft_eef_v1_formal.yaml)，旧单条配置不覆盖。
- 6000步约8.14轮窗口曝光；在2000/4000/6000步完成后评估生成动作，再按相同6000步学习率日程恢复。每阶段保持同一主W&B run；20→21步预检恢复沿用同一个预检run（与主训练独立），同时验证W&B步数连续性。

数据位于6000D `artifacts/20261016-v1-formal-training/dataset/{train,validation}`。H20目标为 `/u01/user/datasets/f1_toys_v1_formal_20261016/dataset/{train,validation}`。数据包自身保留`formal_training_ready=false`，表示生成时尚未通过真实H20预检；预检证据另存run目录，不反写冻结包。

| 身份 | SHA256 |
|---|---|
| 训练manifest | `3427811e84a0df330dc2f3f0ab099c4231883a9e2cf31c106516c6894238e914` |
| 验证manifest | `97724befe1e681d57b9648d179bc74488bf68ee42106600b897e37fe4169279e` |
| 共享contract | `18cb0f682f1e330c657ee5ab727eab459c5c53e8e1e4b3b7c11002fc42313c80` |
| train-only统计 | `b7dafb6adf418518dd7e95b93cca6e0b93242a044aa80393b5e25d71fae47e1d` |
| split | `92ef1706bc0dab10dfafd605ce293e534987700aeaa3684cbd563e6bce7c8aed` |

## 新增数据与验证接线

[prepare_formal.py](../../scripts/prepare_formal.py)逐条再次审计本机原件与索引，校验所有取用JPEG字节、RGB和分辨率；保留发布目标、源episode、观测及动作时间/序号。状态去重使用`(episode, observation_id)`，不让不同episode相同帧号相互覆盖；统计只拟合训练部分，验证拷贝同一统计文件。

reader拒绝多个独立路径被静默只取首个，也拒绝训练读取validation包。验证检查split、统计和contract一致且来源episode互斥。固定每条验证示教均匀取96个起点，共288个；八卡每卡batch2恰好18批，不补重复样本、不丢尾部。

本地RLinf原VLA worker的内置验证未实现；本次只为`pi05_f1_toys`添加固定随机种子的held-out flow loss，按真实样本数加权归约。评估前后恢复Python/NumPy/CPU/CUDA RNG与train/eval模式，不改变训练随机序列。FM loss不是任务成功率；阶段生成评估另查每条验证示教5个时刻、两种噪声种子、5/10采样步的EEF毫米/旋转/夹爪、前20步/全50步和既有IK拒绝率，不重开IK算法研究。

## 已完成的数据检查与限制

原件传输15条全部hash通过。CPU回读75个代表窗口，三图确定性处理与推理路径相同，padding动作维为零，归一化/反归一化与FK往返通过，最大矩阵元素误差约1.01e-8。增强读取已检查有限值与224尺寸。相关回归37项通过（时序、EEF、恢复、split与验证RNG）；这不是八卡训练验收。

查看15条各5个时刻的三路模型输入：抽查中保留木块、目标孔和主要操作区域；部分结束撤离动作靠近主图上边缘，腕相机全图保留。预览位于`artifacts/20261016-v1-formal-training/data-check/`，不是逐帧语义审核或任意增强覆盖证明。

验证集右臂状态元素有3.34%超出训练min/max，最大J5偏离6.435°（另J2约0.410°、J6约0.075°）。保留train-only统计，按既有token编码处理并报告；EEF锚点仍用原始物理状态。不能混入验证范围降低该指标，生成评估记录每个起点的状态越界情况。

## H20执行

版本目标 `f1-toys-20261016-v1-formal-r3`。已有训练环境和独立augmentation overlay复用，W&B凭据和通道沿infra；不安装新全局依赖。启动入口[run_v1_formal.sh](../../scripts/run_v1_formal.sh)在新run目录执行，拒绝覆盖旧实验，预检或恢复失败立即停止，不能继续主训练。

H20命令（由6000D通过SSH启动独立tmux）：

```bash
export F1_FORMAL_RUN_ROOT=/u01/user/runs/f1-toys-v1-formal-20261016-r3
export F1_FORMAL_DATA_ROOT=/u01/user/datasets/f1_toys_v1_formal_20261016/dataset
bash /home/user/projects/task-releases/f1-toys-20261016-v1-formal-r3/tasks/F1_ComplexyToys/scripts/run_v1_formal.sh
```

主run为[在线训练](https://wandb.ai/robotics-example/f1_complexytoys/runs/f1v1formal20261016main1)，已通过API核验。训练日志为run根的`pipeline.log`、`preflight.log`、`resume-check.log`和`train-to-{2000,4000,6000}.log`；checkpoint在`train/v1_formal_eef_lora/checkpoints/global_step_N/actor`。`exit-code.txt`记录流程最终退出码。训练完不会自动控制机器人或替换6000D线上模型。

## 启动预检记录

r2首次八卡预检在第10步验证后因 `Timer run_eval has not been recorded` 退出；新增验证覆写遗漏了RLinf worker计时上下文，runner取耗时时失败。训练已实际执行，loss/梯度有限；没有启动主训练。r3补齐与基类相同的 `worker_timer()`，新目录重做预检。旧run与W&B `f1v1formal20261016pre1`保留，不覆盖失败证据。

r3八卡预检完整20步通过：末步train/loss约0.957，eval/loss约0.952、eval/samples=288；热态约0.83秒/步。恢复第21步train/loss约1.05、eval/loss约0.950，优化器action_out_proj的step由20增至21，全部8个数据cursor由20增至21。证据为新run的`resume-verification.json`；这些是流程预检指标，不是正式训练效果。

正式主训练启动验收：北京时间2026-10-16 16:48，W&B状态running、连续62行（step0–61）、global16、优化器日程6000；最后采样train/loss=0.6970256、grad_norm=2.406019，尚在warmup。八卡显存61,775–62,015MiB、利用率61–99%。初段热态约0.83秒/步；6000步纯训练粗估83分钟，另加保存、验证、阶段生成和重新加载，不能据初段loss声称收敛。实际状态取启动回执与在线run。

H20任务文档镜像可更新到`f1-toys-20261016-v1-formal-running`，主训练始终使用已冻结的formal-r3，不修改运行中的代码。6000D现有单条模型服务保持原状，下一阶段需评估新ckpt再导出。

2026-10-16部署500步时发现阶段生成脚本的权重路径漏了`model_state_dict/`。6000D源入口已修正；正在运行的不可变r3使用run目录中2000/4000/6000的`actor/full_weights.pt → model_state_dict/full_weights.pt`兼容链接，不改训练代码、模型、优化器或日程。目录存在不代表checkpoint完成，后台导出以最后写入的`rng.pt`为门槛。回执为`phase-weight-path-compatibility.json`。

## 用户授权延长到50K

2026-10-16检查时原流程训练至4677步，完整checkpoint到4500；2000/4000固定验证loss约0.0813/0.106，5步生成前20步位置误差8.36/7.50mm、旋转2.25/1.97度、IK成功率均1.0。指标方向不同，保留旧checkpoint，不以更长训练保证效果。新冻结版本`f1-toys-20261016-v1-50k`队列等待原6000流程正常结束，恢复同一run到50000步；0–6000保持原曲线，之后hold1e-5。详见根recipe。

## 2026-10-19续训补验

10月16日自动审批因工具额度限制未执行测试/启动；50K当时并未排队，旧文档该表述已纠正。10月19日确认原流程exit-code=0、6000完整checkpoint与生成评估存在。新版本`f1-toys-20261019-v1-50k`实际环境两项测试通过（原轨迹一致、6000后保持末端、再次恢复不反弹）。

学习率核查：AdamW组lr=1e-4而defaults=1e-3，上游min_lr=1e-5折成0.01，实际6000步lr=1e-6；前次1e-5描述不正确。恢复显式min_lr_rate=0.01/min_lr=null并钳制schedule时间至6000，保持1e-6，不更改旧权重和历史指标。续训仍同W&B run、global16/八卡、每500步存档/验证。

本轮实际恢复已越过6000：6039/50000，train/loss约0.016、grad_norm约0.138、LR=1e-6、热态0.81秒/步，日志无Traceback。续训并未在10月16日自动运行；本次为10月19日实际补启动。
