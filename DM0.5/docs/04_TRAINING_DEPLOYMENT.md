# DM0.5 × G2 冻结训练与部署合同

## 1. 文档职责

本文是模型输入、动作表示、训练配置和部署安全边界的唯一文字版权威来源。
逐维名称与顺序以`g2_sort/config/action_contract.json`为最终机器可读合同。

## 2. 模型输入与图像

每个样本固定包含：

1. `images_head`：头部RGB；
2. `images_left_wrist`：左腕RGB；
3. `images_right_wrist`：右腕RGB；
4. 当前34D measured joint-position state；
5. 固定英文prompt。

图像不离线转成逐帧PNG，也不主动裁边。训练和推理统一执行：

```text
原始MP4帧 → 保持宽高比补黑边成正方形 → resize 448×448
```

不加入深度、雷达、IMU、速度、effort、wrench、头、腰或底盘状态。

## 3. State、action与prompt

state和action都固定为以下34D顺序：

```text
左臂7 + 右臂7 + 左手10 + 右手10
```

H5来源：

```text
state/joint/position             14D
state/left_effector/position     10D
state/right_effector/position    10D

action/joint/position            14D
action/left_effector/position    10D
action/right_effector/position   10D
```

`left/right_effector/position`在本数据中表示灵巧手各10个自由度，不是末端
XYZ。action语义是absolute joint-position，不是delta、velocity或torque。
EEF只保留作诊断，不进入模型。

模型输出固定为`50 × 34`未来绝对目标。训练JSONL必须显式包含`action`，
不得使用未来state代替action。

动作时间语义同时冻结为约30Hz：50个输出表示50个连续的`1/30s`控制区间，
总覆盖时间约1.67s。这个时间网格属于训练数据和模型输出，部署时不得为了匹配
机器人接口而压缩成1s、改成50Hz或复制成固定factor 2。

G2的100Hz只属于域控制器本地`joint_servo_control`。客户端以连续时间对相邻
30Hz absolute-position waypoint做线性插值；原始waypoint、动作顺序和总时长
保持不变。推理请求频率是另一条由HTTP延迟与滚动调度决定的时钟。详细实现和
失败行为见`12_G2_CLIENT_DM05_RUNTIME.md`。

固定prompt：

```text
Use the left hand to pick up each bottle, hand it to the right hand, then place the Sprite bottle in the left tray compartment and the orange juice bottle in the right tray compartment.
```

本轮不做多prompt或语言泛化。

## 4. 数据合同

- 三个归档只是来源，不是train/validation/test；
- 自动准入通过的完整episode合并后，固定为800条train和80条validation；
- split按AID、软件版本和task分层，固定seed为`dm05-g2-v1`；
- 同一episode不得跨split；
- norm stats只由train split计算；
- 三路相机必须通过`.txt`时间戳与H5精确映射；
- 缺H5、关键文件、时间戳或非有限34D数值的episode必须排除；
- 不从视频伪造缺失action。

数据事实和当前统计只在`03_DATA_AUDIT.md`维护。

## 5. 模型与训练

- OpenDM固定commit：
  `6ce8e7f13e0c46906b141fda1773364f18a577c2`；
- DM0.5 base + LoRA；
- 官方32D动作投影先加载原权重，再扩展到34D；
- LoRA训练主体，同时完整训练34D action projections及官方规定的time
  modules；
- RTX 6000D正式训练固定使用官方镜像与上游模型实现，只覆盖一项不兼容内核：
  LLM `sdpa`、vision `flash_attention_2`、action `sdpa`；
- 上游默认LLM `flex_attention`在固定官方镜像中可forward但无法backward：
  Triton内核要求114,688 bytes shared memory，高于本机单SM上限101,376 bytes；
  这不是显存不足，也不能通过减小batch解决；
- 该覆盖不改变权重形状、attention语义、训练目标或checkpoint合同；视觉FA2仍启用；
- 初始目标50k step，每5k step保存；
- 100k是有证据时的上限，不是默认目标。

候选多卡配置保持global batch 32：

| GPU数 | per-device batch | accumulation | global batch |
|---:|---:|---:|---:|
| 8 | 4 | 1 | 32 |
| 4 | 4 | 2 | 32 |

如果OOM，降低per-device batch并提高accumulation，但保持global batch
可比。正式训练前必须用同一冻结子集跑500～1000 step，记录吞吐、显存、
通信、dataloader等待、loss和梯度有限性。

## 6. 模型验收顺序

有完整base后必须依次验证：

1. 官方demo inference；
2. G2单batch forward；
3. G2单batch backward；
4. 新增第33/34维梯度非零；
5. 10～100 step小样本LoRA；
6. checkpoint保存；
7. 新进程恢复；
8. base + LoRA + 34D密集模块加载；
9. HTTP返回`50×34`；
10. norm stats反归一化正常。

单元测试或无权重smoke通过，不能描述成模型训练已经验证。

## 7. 每次实验必须归档

- 项目commit和工作树diff；
- OpenDM commit；
- Docker image ID；
- 数据manifest及hash；
- split、norm stats和34D合同；
- 完整CLI参数；
- GPU型号、world size和global batch；
- step/s、samples/s、峰值显存和dataloader等待；
- checkpoint与恢复结果。

## 8. 部署与安全

部署主线：

```text
G2三路RGB + 34D measured state
          ↓
服务器DM0.5 HTTP推理
          ↓
50×34 absolute joint action chunk
          ↓
G2本地安全控制与滚动执行
```

服务器只负责模型推理。G2本地控制器负责：

- 34D joint index映射和单位转换；
- 插值及位置/速度限幅；
- watchdog和超时safe hold/stop；
- 只执行chunk前缀并滚动重规划；
- 网络异常时停止，不继续执行旧chunk。

在G2 SDK的索引、单位、频率、限位、急停和watchdog全部核对前，只允许
shadow mode，不得向执行器发送模型动作。

客户端运行时、shadow/powered-sync/powered-async和30→100Hz插值的唯一详细
来源是`12_G2_CLIENT_DM05_RUNTIME.md`。
