# G2 原始数据与 H5 字段说明

更新时间：2026-07-27

本文解释G2饮料瓶分拣数据中每类原始文件、`aligned_joints.h5`的实际字段、
各字段的形状和语义，以及它们在当前DM0.5基线或未来具身任务中的用途。

## 一、H5不是全部原始数据

一个完整原始episode的结构大致为：

```text
<episode>/
├── meta_info.json
├── camera/
│   ├── head_color/
│   ├── hand_left_color/
│   ├── hand_right_color/
│   ├── head_stereo_left/
│   ├── head_stereo_right/
│   ├── head_left_fisheye/
│   ├── head_right_fisheye/
│   ├── head_back_fisheye/
│   └── head_depth/
├── record/
│   ├── aligned_joints.h5
│   └── *.pbdat
├── parameters/
│   ├── hardware/
│   └── sensor/
└── logs/
```

- `camera/`：原始H.265视频、深度数据和每路传感器时间戳；
- `record/*.pbdat`：机器人模块产生的原始protobuf消息流；
- `record/aligned_joints.h5`：从原始消息流中整理出的、按统一时间轴对齐的
  机器人状态、控制命令和传感器时间戳；
- `parameters/hardware/`：机械臂、灵巧手、头部、底盘和控制器版本；
- `parameters/sensor/`：相机、雷达和IMU内外参；
- `logs/`：状态机、定位、自由空间、性能和OTA等运行日志；
- `meta_info.json`：机器人、软件版本、任务、时长、末端执行器和质量验证。

因此，H5是训练最方便使用的“对齐结构化数据”，不是一次采集的全部raw。
如果H5字段有问题或需要重新定义同步方式，才需要回到`.pbdat`重新生成。

## 二、当前DM0.5实际使用的数据

第一版每个训练时间步使用：

```text
输入 observation
├── 头部RGB
├── 左腕RGB
├── 右腕RGB
├── 当前34D实测state
└── 固定英文prompt

监督目标
└── 未来50步 × 34D绝对关节action
```

三路图像不需要提前保存为PNG。原始H.265无重编码封装成MP4后，JSONL记录
视频路径和`frame_idx`；OpenDM在线解码对应RGB帧，再补边、resize到
`448×448`并转换为VLM输入tensor。

34D由以下字段拼接：

```text
state/joint/position             14D  双臂实测位置
state/left_effector/position     10D  左手实测位置
state/right_effector/position    10D  右手实测位置

action/joint/position            14D  双臂绝对目标
action/left_effector/position    10D  左手绝对目标
action/right_effector/position   10D  右手绝对目标
```

顺序固定为：

```text
左臂7 + 右臂7 + 左手10 + 右手10
```

`left_effector/position`和`right_effector/position`虽然带有`effector`名称，
在这份数据中实际是左右灵巧手各10个自由度，不是机械臂末端XYZ。

## 三、“关节位置”为什么不是三维坐标

机器人数据中的`position`需要结合对象理解：

- 旋转关节的position：一个关节角，是一个标量；
- 移动关节的position：沿关节轴的位移，也是一个标量；
- 笛卡尔空间中一个点的位置：`[x, y, z]`三个标量；
- 刚体完整姿态：还需要四元数`[x, y, z, w]`或其他旋转表示。

G2一只手臂有7个旋转关节，因此：

```text
7个关节角
    ↓ 正向运动学 FK
末端XYZ位置 + 三维姿态
```

所以`state/joint/position`的14个数是左右臂各7个关节角。它们不是14个
三维点，而是14个关节各自的一个广义坐标。真正的左右末端三维位置保存在
`state/end/position`，形状是`2×3`；末端姿态保存在
`state/end/orientation`，形状是`2×4`四元数。

## 四、为什么第一版只用position

当前任务是固定底盘、固定头腰、低速双瓶抓取和双手交接。采集控制器下发的
主要标签也是绝对关节位置目标，而不是速度或力矩目标。对这种准静态模仿学习
任务，使用当前关节位置作为本体状态、预测未来位置chunk，是常见且较稳的
第一版方案。

这不表示速度和力矩“不管”：

- 训练数据审计：检查速度/力矩尖峰、碰撞、卡住和异常轨迹；
- 真机安全：本地控制器必须限制位置、速度、加速度和力矩；
- 动作执行：DM0.5输出位置目标，本地控制器负责插值和闭环跟踪；
- 故障分析：模型失败时利用速度、effort和wrench判断是视觉、策略、控制
  还是机械接触问题。

第一版不把速度/力矩送进模型的原因：

1. 原始监督action是位置目标，没有对应的速度或力矩action标签；
2. 当前任务速度较低，position已能描述最主要的机器人构型；
3. 增加输入会改变state维度、norm stats、训练合同和真机请求格式；
4. 第一版目标是先验证最小、可解释的DM0.5能力，不同时扩展多个变量。

以下任务更可能需要将速度、力或触觉正式纳入模型：

- 快速动态操作、抛接或高速跟踪；
- 插拔、装配、拧紧等强接触任务；
- 柔顺抓取易碎或易变形物体；
- 仅靠位置无法区分“正在运动”和“被卡住”的状态；
- 需要直接预测速度、力矩或阻抗参数的控制策略。

即使以后加入这些输入，也应先确认DM0.5状态编码、归一化、训练接口和真机
控制器能共同支持，不能只在JSONL尾部随意追加。

## 五、H5时间与相机字段

| 字段 | 形状 | 含义 | 当前用途 |
|---|---:|---|---|
| `main_timestamp` | 标量 | 当前H5时间步的主时钟 | 连续性检查、同步基准 |
| `timestamp/camera/head_color` | 1 | 对应头部RGB原始时间戳 | 训练三路RGB映射 |
| `timestamp/camera/hand_left_color` | 1 | 对应左腕RGB原始时间戳 | 训练三路RGB映射 |
| `timestamp/camera/hand_right_color` | 1 | 对应右腕RGB原始时间戳 | 训练三路RGB映射 |
| `timestamp/camera/head_depth` | 1 | 对应头部深度帧 | 当前不用；3D感知可用 |
| `timestamp/camera/head_stereo_left` | 1 | 对应左双目帧 | 当前不用 |
| `timestamp/camera/head_stereo_right` | 1 | 对应右双目帧 | 当前不用 |

H5顶层键`0, 1, 2, ...`只是对齐时间步编号，不等于原始视频帧号。转换时必须
用上述相机时间戳在各相机`.txt`中查找真实`frame_idx`。

## 六、双臂关节字段

| 字段 | 形状 | 含义 | 当前/未来用途 |
|---|---:|---|---|
| `state/joint/position` | 14 | 左右臂各7个实测关节位置 | 当前DM0.5 state |
| `state/joint/velocity` | 14 | 双臂关节速度 | 动态状态、异常检测、控制调试 |
| `state/joint/effort` | 14 | 关节负载/力矩反馈 | 碰撞、卡住、接触与机械诊断 |
| `state/joint/mode` | 14 | 各关节控制/工作模式 | 数据过滤、故障和安全检查 |
| `action/joint/position` | 14 | 采集控制器下发的绝对位置目标 | 当前DM0.5 action标签 |

`effort`的物理单位和是否等同于关节输出力矩需要以G2 SDK合同为准，不能只
根据字段名假设。当前将其作为诊断量，不作为模型监督目标。

## 七、左右灵巧手字段

| 字段 | 形状 | 含义 | 当前用途 |
|---|---:|---|---|
| `state/left_effector/position` | 10 | 左手10D实测位置 | 当前DM0.5 state |
| `state/right_effector/position` | 10 | 右手10D实测位置 | 当前DM0.5 state |
| `action/left_effector/position` | 10 | 左手10D绝对目标 | 当前DM0.5 action |
| `action/right_effector/position` | 10 | 右手10D绝对目标 | 当前DM0.5 action |

每只手的10D合同为：

```text
thumb_roll
thumb_abad
thumb_mcp
index_abad
index_pip
middle_pip
ring_abad
ring_pip
pinky_abad
pinky_pip
```

## 八、EEF笛卡尔字段

EEF（end effector）是机械臂末端在笛卡尔空间中的表示。

| 字段 | 形状 | 含义 | 什么时候使用 |
|---|---:|---|---|
| `state/end/position` | 2×3 | 左右末端XYZ | EEF策略、FK/IK检查 |
| `state/end/orientation` | 2×4 | 左右末端四元数 | EEF姿态控制 |
| `state/end/arm_position` | 2×3 | 机械臂末端位置 | 运动学诊断 |
| `state/end/arm_orientation` | 2×4 | 机械臂末端姿态 | 运动学诊断 |
| `state/end/pose` | 28 | 多个末端frame的组合pose | 厂商控制链诊断 |
| `state/end/velocity` | 24 | 多个末端frame的线/角速度 | 动态控制、调试 |
| `state/end/wrench` | 24 | 多个末端frame的力/力矩 | 接触、插拔、装配 |
| `action/end/position` | 2×3 | 目标末端XYZ | 训练EEF action时 |
| `action/end/orientation` | 2×4 | 目标末端四元数 | 训练EEF action时 |
| `action/left_refpose/orientation` | 4 | 左侧参考四元数 | IK/遥操作映射 |
| `action/right_refpose/orientation` | 4 | 右侧参考四元数 | IK/遥操作映射 |
| `state/end/errcode` | 1 | 末端控制错误码 | 故障过滤 |
| `state/end/errmsg` | 1 | 错误文本 | 故障诊断 |
| `state/end/mode` | 1 | 末端控制模式 | 合同与安全检查 |

第一版选择joint action，因此这些字段不进入模型。如果以后改成EEF策略，
必须先确认：

- XYZ所属坐标系；
- 左右手frame定义；
- 四元数顺序；
- 单位；
- IK解算器；
- 奇异位形和关节限位；
- 训练与真机使用完全相同的坐标变换。

`record.tar.gz`与另外两个归档的部分EEF诊断字段结构不同，也进一步说明
第一版不应混用这些EEF字段。

## 九、头部与腰部字段

| 字段 | 形状 | 含义 | 什么时候使用 |
|---|---:|---|---|
| `state/head/position` | 3 | 头部3个关节的位置 | 主动视觉、转头任务 |
| `state/head/velocity` | 3 | 头部关节速度 | 主动视觉控制 |
| `state/head/effort` | 3 | 头部负载反馈 | 故障诊断 |
| `state/head/mode` | 3 | 头部控制模式 | 状态与安全检查 |
| `action/head/position` | 3 | 头部目标位置 | 模型同时控制头部时 |
| `state/waist/position` | 5 | 腰部5D位置 | 弯腰、扩大工作空间 |
| `state/waist/velocity` | 5 | 腰部速度 | 动态控制 |
| `state/waist/effort` | 5 | 腰部负载反馈 | 安全和机械诊断 |
| `state/waist/mode` | 5 | 腰部控制模式 | 状态检查 |
| `action/waist/position` | 5 | 腰部目标位置 | 模型同时控制腰部时 |

当前任务头部、腰部和身体固定，因此全部不进入第一版模型。如果以后要转头
寻找物体或弯腰扩大工作区，需要扩充state/action合同并重新训练。

## 十、机器人本体字段

| 字段 | 形状 | 含义 | 什么时候使用 |
|---|---:|---|---|
| `state/robot/position` | 3 | 本体XYZ位置 | 移动操作、导航 |
| `state/robot/orientation` | 4 | 本体四元数 | 定位、移动操作 |
| `action/robot/velocity` | 6 | 本体线速度和角速度命令 | 移动底盘策略 |

当前G2底盘固定，不使用这些字段。移动取物、跨工位操作或导航任务才需要。

## 十一、H5以外的传感器和原始流

| 数据 | 当前是否使用 | 适用任务 |
|---|---|---|
| 激光雷达 | 否 | SLAM、导航、动态避障 |
| 头部深度 | 否 | 点云、3D抓取、距离估计 |
| 双目/鱼眼 | 否 | 大范围环境感知、导航、减少遮挡 |
| IMU | 否 | 移动、平衡、定位 |
| 关节effort/EEF wrench | 仅诊断 | 接触、插拔、装配、柔顺控制 |
| 标定参数 | 转换/诊断时使用 | 3D融合、坐标变换、投影 |
| `.pbdat` | 否 | 重建H5、恢复未对齐的原始topic |
| `.INFO`日志 | 否 | 系统故障和采集问题定位 |

## 十二、当前具身项目需要优先掌握的概念

1. **Observation与action**：state是实际反馈，action是控制器目标。
2. **关节空间与笛卡尔空间**：关节角不是XYZ；FK把关节角转换为末端pose，
   IK执行相反方向的求解。
3. **时间同步**：图像、state和action必须对应同一时间轴。
4. **动作表示**：absolute joint、relative joint、EEF和velocity action不能
   混用。
5. **归一化**：训练集norm stats必须随checkpoint交付，推理输出必须反归一化。
6. **控制分层**：VLA预测action chunk，本地控制器负责插值、闭环、限位和急停。
7. **数据划分**：必须按完整episode切分，不能把相邻帧随机分到训练和验证。
8. **真机合同**：关节顺序、单位、频率、坐标系和安全限制必须训练部署一致。
9. **成功与质量**：任务最终成功和轨迹是否平滑、是否碰撞是不同维度。
10. **失败诊断**：结合视频、state/action、velocity、effort、wrench和控制日志
    判断失败来自感知、策略、控制还是机械问题。

当前baseline不需要一次性把雷达、深度、EEF、速度和力矩全部喂给模型。
先用三路RGB加34D位置合同验证能力；只有真实失败证据表明缺少某类信息时，
再有针对性地扩展输入或控制表示。

## 十三、本地真实样本入口

完整字段树和三帧实际数值位于：

```text
data/raw_samples/inspections/record.tar.gz/
data/raw_samples/inspections/record_20260409.zip/
data/raw_samples/inspections/record_20260413.tar.gz/
```

其中：

- `h5_structure.txt`：首帧全部字段、shape和dtype；
- `h5_real_frames.json`：第0帧、中间帧、最后一帧的全部真实数值。

原始H5保持不变；这些文本和JSON只是方便人工查看的只读导出。
