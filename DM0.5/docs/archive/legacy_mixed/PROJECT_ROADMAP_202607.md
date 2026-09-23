---
doc_type: archive
domain: operations
status: archived
created: 2026-07-28
updated: 2026-08-24
models: [dm05, old_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: [../../CURRENT_STATUS.md]
related: []
---

# DM0.5 × G2 项目目标与路线图

## 1. 文档职责

本文只回答三个问题：项目要完成什么、分几个阶段、如何验收。具体配置以
`04_TRAINING_DEPLOYMENT.md`为准，数据事实以`03_DATA_AUDIT.md`为准，当前
进度以`02_IMPLEMENTATION_STATUS.md`为准。

## 2. 最终目标

使用固定版本OpenDM/DM0.5，通过LoRA适配智元G2双臂灵巧手数据，在固定工位
完成完整饮料瓶分拣：

1. 左手抓取雪碧并交给右手；
2. 右手把雪碧放入托盘左侧；
3. 左手抓取橙汁并交给右手；
4. 右手把橙汁放入托盘右侧。

最终固定模型、工位和prompt执行20次完整任务，报告整体成功率、各阶段成功率
和失败类型。离线loss只用于工程检查和checkpoint筛选，不能替代真机结果。

## 3. 项目范围

本轮固定：

- 三路RGB：头部、左腕、右腕；
- 34D measured joint-position state；
- 34D absolute joint-position action；
- 50步action chunk；
- 单一英文prompt；
- DM0.5 base + LoRA，并训练34D动作投影及规定的密集模块；
- 完整episode级800 train / 80 validation分层划分；
- 服务器训练和推理，G2本地负责安全控制。

本轮不做：

- LIBERO benchmark；
- EEF、relative action、速度、力矩或wrench模型；
- 深度、雷达、IMU、头、腰或底盘输入；
- 多prompt或场景泛化；
- Thor ARM64本地推理；
- 未核对G2 SDK合同前的powered motion。

冻结字段、prompt、batch和安全要求只在
`04_TRAINING_DEPLOYMENT.md`及
`g2_sort/config/action_contract.json`维护，本文不重复展开。

## 4. 实施阶段

### A. 工程基线

- 固定OpenDM commit和项目仓库；
- 完成34D动作头、G2数据集、LoRA配置和回归测试；
- 建立可复现Docker环境。

### B. 数据准入

- 三个原始归档复制到服务器本地NVMe；
- 生成归档索引；
- 只提取训练必需字段；
- 对每个episode执行结构、H5、时间戳和视频轻量检查；
- 冻结有效episode manifest及800/80 split；
- 仅用train split计算norm stats。

### C. 模型smoke

- 校验固定revision的DM0.5 base；
- 官方demo inference；
- G2单batch forward/backward；
- 10～100 step小样本LoRA；
- checkpoint保存、恢复和HTTP输出`50×34`验证。

### D. 服务器基准与正式训练

- 用冻结子集跑500～1000 step；
- 比较4卡和8卡吞吐、显存、通信及dataloader等待；
- 保持global batch 32可比；
- 正式训练先以50k step为目标，每5k step保存；
- 只有验证与早期真机证据仍改善时才考虑100k上限。

### E. 部署与真机验证

- 先运行shadow mode；
- 再依次验证低速空载、单瓶抓取、交接、放置和完整双瓶流程；
- G2本地执行插值、限位、watchdog、超时停止和滚动重规划；
- 最终完成20次固定条件评估。

当前处于哪个阶段以及下一道门槛，见
`02_IMPLEMENTATION_STATUS.md`。

## 5. 阶段验收

| 阶段 | 必须产物 |
|---|---|
| 工程基线 | 固定源码、Docker image ID、34D测试结果 |
| 数据准入 | 归档索引、审计报告、有效manifest、split hash、norm stats |
| 模型smoke | forward/backward、短LoRA、checkpoint round-trip证据 |
| 正式训练 | 完整配置、吞吐记录、checkpoint清单、validation结果 |
| 部署 | base/LoRA/密集模块、norm stats、HTTP与shadow日志 |
| 真机 | 20次视频、状态、动作、安全过滤结果和失败分类 |

## 6. 权威信息顺序

发生冲突时按以下顺序处理：

1. 用户当前明确指令；
2. `g2_sort/config/action_contract.json`；
3. `04_TRAINING_DEPLOYMENT.md`；
4. `03_DATA_AUDIT.md`；
5. `02_IMPLEMENTATION_STATUS.md`；
6. 本路线图和其他说明文档。

任何冻结合同变更都必须记录证据、影响范围以及是否需要重新处理数据或训练。
