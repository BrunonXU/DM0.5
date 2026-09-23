---
doc_type: principle
domain: inference
status: canonical
created: 2026-07-29
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [checkpoint_selection_to_robot]
supersedes: [11_CHECKPOINT_SELECTION_TO_ROBOT.md]
superseded_by: []
related: [../experiments/training/]
---

# Checkpoint选型到真机闭环验收流程

更新时间：2026-07-29

## 1. 文档职责

本文冻结从正式训练产物中选择checkpoint，经过离线模型验收、shadow mode、
分级powered test，最终进入真机闭环的顺序。它回答三个问题：

1. 哪些步骤是进入下一阶段的必要门禁；
2. 哪些工作有价值但不阻塞第一轮验证；
3. 哪些常见做法不能作为模型有效的证据。

模型输入、34D动作合同和安全边界仍以`TRAINING_AND_DEPLOYMENT_CONTRACT.md`为准。
G2客户端模块、DM0.5 HTTP、30/100Hz时钟、同步hold和异步提前请求的实现细节
统一见`RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md`和
`../robot/RTC_PROCESS_ISOLATION.md`，本文不重复维护客户端算法。

## 2. 总原则

```text
train loss
    ↓ 只证明优化过程与训练集拟合
固定validation checkpoint sweep
    ↓ 选择候选模型
离线生成动作与安全合同
    ↓ 取得shadow资格
真机shadow（预测但不执行）
    ↓ 取得低风险powered test资格
分级、限速、可急停的powered test
    ↓ 取得完整任务资格
滚动重规划闭环 + 重复试验
```

不能从“train loss很低”或“validation loss最好”直接跳到完整真机动作执行。
validation解决的是未见episode上的离线泛化，不能覆盖控制延迟、观测漂移、
动作误差累积、网络异常和分布外状态。

## 3. 阶段A：训练健康度

### 必要

- loss、FM loss和gradient norm全部有限；
- 无OOM、数据读取失败、rank退出或checkpoint损坏；
- 学习率与冻结scheduler一致；
- global batch、GPU数和实际step连续；
- 每个保存点包含adapter、34D密集模块、norm stats、optimizer、scheduler和
  所有rank的RNG。

DM0.5当前`loss == fm_loss`，是归一化动作空间中预测流场`v_t`与目标
`noise - action`之间的MSE。随机flow time和noise会造成单步波动。因此：

- 单步loss上升不等于过拟合；
- loss `0.01`不等于1%动作误差、0.01rad或99%成功率；
- 观察至少数百步的滑动均值，而不是相邻两步；
- 过拟合主要由train/validation分叉确认。

### 不必要

- 要求train loss降到0；
- 因一次尖峰立刻停训；
- 默认最后一个checkpoint最好；
- 在50K以后没有证据地直接扩展到100K。

## 4. 阶段B：冻结checkpoint对比协议

这是必要门禁，必须在查看各checkpoint结果前冻结，防止按结果反复修改评估。

候选集合：

```text
checkpoint-5000, 10000, 15000, ..., 50000
```

所有候选必须使用完全相同的：

- 80条validation episode manifest；
- train-only norm stats，禁止计算validation统计；
- 官方固定Docker和同一attention组合；
- 输入图像处理、prompt、34D state/action顺序；
- inference diffusion steps；
- 样本索引和随机seed；
- batch size及指标实现。

由于FM评估会采样noise和flow time，每个固定样本至少使用多个固定seed。汇总时
先按episode聚合，再对episode做macro average，避免长episode凭帧数支配总分。

### 推荐但不阻塞第一轮

- 从未查看指标的validation中预留一小部分final holdout，或额外采一批新数据；
- 对episode bootstrap计算置信区间；
- 保留数据来源、软件版本、task和任务阶段的分层结果。

如果80条全部用于checkpoint选择，它们仍可用于相对排名，但不能再被描述为
完全无偏的最终test set。

## 5. 阶段C：Validation指标

### C1. 必要：固定seed的validation FM loss

每个checkpoint至少报告：

- mean、median、P90；
- 每episode均值及最差episode；
- 四组动作：左臂7D、右臂7D、左手10D、右手10D；
- 各数据来源和任务阶段。

判断：

- train下降且validation同步下降：仍在改善；
- train下降、validation连续两个以上checkpoint恶化：过拟合信号；
- validation连续2～3个checkpoint变化很小：进入平台期；
- 均值变好但P90/最差episode明显恶化：不能只看均值选型。

### C2. 必要：完整动作生成指标

FM loss是训练目标，不等于最终10步flow推理得到的`50×34`动作质量。必须调用
与部署相同的`inference_action()`，反归一化后与录制absolute action比较：

- MAE、RMSE；
- 第1、5、10、25、50 horizon误差；
- 左右臂、左右手分别统计；
- chunk首动作相对当前state的跳变；
- 速度、加速度、jerk；
- 训练分位范围及物理关节限位越界率；
- NaN/Inf和shape错误率；
- 多个生成seed的均值、方差和最差结果。

单条示范不是唯一合法动作，直接MAE会惩罚部分合理替代轨迹，所以MAE是代理
指标而不是真实成功率。它必须与FM loss、约束违反率和后续shadow共同判断。

### C3. 推荐：输入依赖与鲁棒性消融

- 单独遮挡头部、左腕或右腕相机；
- 冻结或打乱state；
- 替换错误prompt；
- 轻微亮度、位置和背景变化；
- 比较输出变化是否符合任务语义。

如果正常图像与全黑/打乱图像输出几乎一致，应怀疑模型忽略视觉输入。此项强烈
推荐，但第一轮受控shadow前可先用小型固定集合完成。

## 6. Checkpoint选择规则

不要只按一个数字排序。先应用硬门禁：

1. checkpoint合同与新进程加载通过；
2. 输出始终为`50×34`且全部有限；
3. 反归一化、joint order和单位正确；
4. 无关节限位或明显动力学违规；
5. validation FM loss和动作指标没有明显退化簇。

再在通过者中比较：

1. validation episode-macro FM loss与P90；
2. 反归一化动作MAE/RMSE；
3. horizon增长曲线；
4. 四个关节组的最差项；
5. 平滑度和约束裕量。

输出两个产物：

- `primary checkpoint`：综合最好，用于shadow；
- `fallback checkpoint`：更早且稳定，用于回退。

如果20K最佳、30K开始validation恶化，就使用20K；没有规定必须使用50K。

## 7. 阶段D：离线部署合同

进入机器人之前必须完成：

- 全新进程加载base + LoRA + 34D密集模块；
- HTTP/本地推理返回`50×34`；
- train-only norm stats反归一化；
- G2 SDK 34D joint index逐维映射；
- radian/degree及手指单位确认；
- absolute joint-position语义确认；
- 控制周期、相机/状态时间戳和推理延迟测量；
- joint position、velocity、acceleration限幅；
- watchdog、请求超时、safe hold/stop和硬件急停；
- 网络断开后禁止继续执行旧chunk。

### 不必要

- 为第一版添加深度、雷达、EEF或其他未进入训练的传感器；
- 修改为relative action；
- 一次性开环执行全部50步；
- 在未验证34D映射时依靠“动作看起来合理”放行。

## 8. 阶段E：真机shadow mode

这是必要门禁。机器人接收真实三路RGB和34D measured state，服务器实时生成
动作，但控制器不向执行器发送模型动作。

至少记录：

- 输入时间戳、推理耗时、网络时延和丢包；
- 每次`50×34`原始与限幅后动作；
- 当前state到chunk首动作跳变；
- 限位、速度、加速度和watchdog触发；
- 正常、失败恢复和轻微场景变化；
- 人工标记抓取、交接、放置等任务阶段。

通过标准必须在观察结果前冻结。任何NaN、错维度、单位错误、明显反向运动、
高频抖动或频繁限幅，都阻塞powered test。

## 9. 阶段F：分级powered test

每一级都必须保留操作员和急停，失败后回到上一级定位，不直接扩大范围。

1. **无物体低速动作**：验证方向、单位、限位和safe stop；
2. **单臂/单手子任务**：固定瓶位，只验证接近与抓取；
3. **双臂交接**：低速、短chunk前缀、固定初始条件；
4. **单种瓶放置**：分别验证左右托盘目标；
5. **完整固定场景任务**：抓取、交接、分类放置；
6. **受控扰动**：瓶位、光照和初始关节状态的小范围变化。

控制器采用滚动重规划：

```text
观测 → 预测50步 → 安全过滤 → 只执行短前缀 → 重新观测
```

短前缀长度必须结合实际控制频率和延迟实测冻结。禁止第一次powered test就
开环执行完整chunk。

## 10. 阶段G：真机闭环验收

在固定条件和受控变化条件下重复试验，至少记录：

- 抓取成功率；
- 交接成功率；
- Sprite/橙汁正确分类与放置成功率；
- 完整任务成功率；
- 碰撞、掉瓶、错抓、错放、超限和人工干预率；
- 平均完成时间和P90；
- 推理/网络超时与safe stop次数；
- 按失败阶段分类的视频与telemetry。

试验次数、成功阈值和允许干预率应在看结果前由项目冻结。只有完整闭环指标达到
门槛，才能称为任务验证成功；validation最好只能说明取得真机测试资格。

## 11. 哪些是必要、推荐、不必要

| 项目 | 分类 | 原因 |
|---|---|---|
| 全checkpoint固定validation对比 | 必要 | 选择模型并识别过拟合 |
| train-only norm stats用于validation | 必要 | 防止统计泄漏 |
| 部署同路径的完整动作生成 | 必要 | FM loss不等于最终动作 |
| 34D映射、单位、限幅、watchdog、急停 | 必要 | powered motion安全合同 |
| shadow mode | 必要 | 验证真实输入和时延但不驱动 |
| 分级powered test | 必要 | 控制风险并定位失败阶段 |
| 独立final holdout或新采数据 | 推荐 | 提供更无偏的最终离线估计 |
| 多相机/状态消融 | 推荐 | 检查模型是否真正使用输入 |
| TensorBoard或W&B | 推荐但非门禁 | 提升可观测性，不决定模型正确性 |
| train loss降到0 | 不必要 | 可能过拟合且无成功率含义 |
| 默认使用最后checkpoint | 不正确 | 应按validation综合指标选择 |
| 人工观看全部训练帧 | 不必要 | 分层抽查和指标更有效 |
| 首次上机执行完整50步chunk | 禁止 | 无法及时纠偏，风险过高 |

## 12. 当前训练曲线与W&B/TensorBoard

### TensorBoard：已补救并实时运行

当前正式训练启动时没有logger callback，但逐step文本日志完整。因此已经：

1. 将历史日志回填为TensorBoard event；
2. 每5秒追写最新step；
3. 提供loss、FM loss、gradient norm、learning rate和epoch曲线；
4. 只监听服务器`127.0.0.1:6006`，不占GPU。

查看方式：

```bash
# 本地终端建立SSH转发
ssh -L 6006:127.0.0.1:6006 <user>@<server>

# 本地浏览器
http://127.0.0.1:6006
```

使用VS Code Remote SSH时，也可在“端口/Ports”面板转发远程`6006`后点击打开。

服务管理：

```bash
tmux attach -t dm05-curves
curl http://127.0.0.1:6006/data/plugin/scalars/tags
```

### W&B：原生方式应在step 0前启用

最佳方式是在训练启动前完成W&B登录，并冻结project、entity、run ID和
`report_to=wandb`。这样Trainer callback能原生记录配置、step和恢复关系。

本次实际为`report_to=[]`，没有W&B run。现在有两种选择：

- **保持当前TensorBoard**：最安全，已经覆盖历史与实时曲线；
- **W&B旁路回填**：登录后读取同一日志，把历史step上传并继续tail。

旁路W&B不需要中断训练，但它不是Trainer原生run：早期系统指标、callback状态
和部分artifact关系无法自动还原，必须标记为`backfilled from train.log`。
W&B本身不是模型验收必要条件，不能为了图表中断当前正式训练。
