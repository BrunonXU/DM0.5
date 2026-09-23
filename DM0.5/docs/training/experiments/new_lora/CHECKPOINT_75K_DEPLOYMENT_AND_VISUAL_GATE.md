---
doc_type: experiment
domain: inference
status: frozen
created: 2026-08-17
updated: 2026-08-17
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/03_NEWLORA_75K_DEPLOYMENT_AND_VISUAL_GATE.md]
superseded_by: [../../../CURRENT_STATUS.md]
related: [VISUAL_AUG_PAIRED_REPORT.md]
---

# NewLora 75K部署、20/50与视觉验证状态（历史快照）

更新时间：2026-08-17。

> 后续状态：E6-A轻量视觉增强配对已完成，见
> [70K/75K轻量视觉增强配对测试](VISUAL_AUG_PAIRED_REPORT.md)。
> 本文第4--5节保留的是E6-A执行前的部署门禁记录；强视觉反事实E6-B仍未完成。

## 1. 当前部署

离线选拔结果为`75K Primary / 70K Fallback`。75K已部署到6000D：

- launcher：`infra/host/run_NewLora_75k_inference.sh`；
- container：`NewLora-dm05-http-75k`；
- physical GPU：4；
- endpoint：`http://10.130.153.114:7892/process_frame`；
- checkpoint：`NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000`；
- Euler去噪步数：10；
- 返回合同：`50×34` finite absolute actions；
- restart policy：`unless-stopped`。

服务加载checkpoint内的NewLora train-only `norm_stats.json`，而不是OldLora或验证集统计。
这保证模型归一化和反归一化与训练时的34D分布完全一致。

## 2. Relative推理链

G2客户端仍上传三路RGB和当前34D measured state，不上传delta。服务端严格执行：

```text
输入：current state[34]
模型空间：arm action[0:14] - current arm state[0:14]
          hand action[14:34]保持absolute
模型生成：50×34 mixed action
输出反变换：predicted arm delta + current arm state
             predicted hand保持不变
返回G2：50×34 absolute targets
```

训练与推理共用`14×JOINT + 20×GRIPPER`的state description。现有NewLora HTTP配方会
显式注入该描述，避免普通HTTP请求缺少`robot_type/state_desc`时无法还原relative动作。

## 3. 20/50重规划

模型和训练horizon仍为50；只改变G2同步客户端一次实际消费的前缀：

```text
旧基线：一次执行49/50，约1.633秒后重新观测
当前方案：一次执行20/50，约0.667秒后重新观测
```

这样能缩短瓶子偏位、碰倒或抓空后模型看不到新结果的时间。它不能凭空创造恢复策略：
若新观测已经进入新请求，而新chunk仍继续交接/放置，则问题更可能是训练集中缺少恢复
示范、视觉影响弱或机器人state阶段先验过强。

后续加入RTC时必须保持单变量对照。先冻结NewLora 75K、10步Euler、20/50 sync和当前
硬件平滑参数，记录请求采集时刻、响应时刻、实际接入索引；再单独启用RTC。RTC的时域
窗口不能超过仍然新鲜的20步预算，否则会重新引入陈旧动作。

## 4. 图像增强稳定性到底测没测

结论：**训练使用了官方augmentation，但checkpoint选拔没有做增强图像稳定性测试。**

已完成的E2--E5全部使用固定held-out原图和`NoAugmentationPipeline`，目的是让15个
checkpoint看到完全相同的输入，避免随机augmentation污染排名。已报告的seed稳定性是
扩散生成噪声稳定性，不是视觉扰动稳定性。验证报告E6已明确标为“未执行”。

因此当前75K是原图离线指标的Primary，不是已经通过光照/裁剪/旋转鲁棒性门禁的赢家。

## 5. E6应怎样做

使用同一批held-out observations、同一state、prompt和generation seed，对70K与75K做
paired输入。不要每次无记录地随机增强，而应预生成可复现参数和manifest：

1. 原图基线；
2. 亮度和对比度变化，覆盖数采与rollout的实际光照差；
3. 官方5% crop；
4. 官方±2° rotation；
5. 官方color jitter组合；
6. 三路相机一致扰动与逐相机独立扰动分开统计；
7. 黑图、错配episode图、时序打乱图仅做shadow反事实，绝不下发机器人。

核心指标不是“增强图MAE单独多小”，而是同一个checkpoint在`corrupted-original`之间的
动作变化，以及任务相关方向是否合理：

- 前20步手臂目标的paired MAE/P95/P99；
- 首动作jump变化；
- 每关节最大变化和q01/q99外比例变化；
- 多generation seeds下的视觉效应与同图seed噪声之比；
- 瓶子位置变化时，reach方向是否随视觉产生系统变化；
- 黑图/错配图是否仍输出几乎相同的阶段轨迹。

部署后已用一个held-out真实三相机样本完成6000D HTTP smoke：HTTP 200，RTT 0.675秒，返回50×34且全部finite；第一步手臂相对当前state最大差0.02734 rad，证明服务端已输出absolute目标。上线前最小门禁仍是完成G2真实图像Shadow，确认严格50×34和relative反变换正常；E6可与
真机静态采图并行补做，但在完成前不能把真机失败直接归因于硬件。

## 6. 真机门禁

6000D只负责服务和只读诊断，不远程运行任何Servo/powered动作。G2端由Bruno手动：

```bash
cd /home/agi/xza/DM0.5 || exit 1
./scripts/00_preflight.sh
./scripts/01_shadow.sh 3
./scripts/04_powered_guarded_smoke.sh 1 normal
./scripts/04_powered_guarded_smoke.sh 3 normal
./scripts/04_powered_guarded_smoke.sh 6 normal
./scripts/05_powered_sync.sh --duration-seconds 10
```

前三个powered smoke必须有人握住物理急停；任一步首跳、限位、GDK或相机异常都停止，
不自动切low-latency继续。
