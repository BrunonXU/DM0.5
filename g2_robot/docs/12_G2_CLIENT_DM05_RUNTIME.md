# G2 Client → DM0.5 真机运行时

> **文档边界（2026-08-24）：** 本文是G2现场实现快照，不再承担跨仓库架构权威。
> 当前“Control唯一拥有Robot、Policy唯一拥有Camera”的目标架构见
> [`../../DM0.5/docs/robot/RTC_PROCESS_ISOLATION.md`](../../DM0.5/docs/robot/RTC_PROCESS_ISOLATION.md)；
> 30/100Hz与RTC第一性原理见DM0.5 principles。本文若与两者冲突，以DM0.5为准。

> 本文保留最初的架构与时钟约定，不是现场操作手册。当前上机命令、安全按键和
> normal/low-latency选择以`00_G2_OPERATOR_GUIDE.md`为准。

更新时间：2026-08-04

## 1. 文档职责

本文是 G2 客户端运行时、DM0.5 HTTP 合同、30/100Hz 时钟和三阶段真机接入
的唯一详细来源。模型训练合同读`04_TRAINING_DEPLOYMENT.md`，checkpoint选择
门禁读`11_CHECKPOINT_SELECTION_TO_ROBOT.md`。

第一版冻结条件：

- 三路 RGB，顺序为头、左腕、右腕；
- `g2_sort.config.dm05_contract.PROMPT`中的固定英文 prompt；
- 34D measured state；
- `50×34` absolute joint-position action；
- 模型动作时间网格 30Hz；
- G2本地Split下发：双臂`joint_servo_control` 100Hz，O10双手
  `move_ee_pos(dual_tool,o10_t2)` 5Hz；
- 服务器选择 checkpoint 和 norm stats，客户端只知道 HTTP endpoint；
- 网络、相机、state 或 action 合同失败时 hold，永不外推旧 chunk。

## 2. 四台角色的职责

```text
笔记本
  │ SSH：启动/停止/看日志；不加载模型，也不转发每个动作
  ▼
G2 域控制器
  ├─ g2_sdk_client.py
  ├─ 三路 GDK 相机 + 严格 34D measured state
  ├─ DM0.5 HTTP client
  ├─ shadow/sync/async 调度
  └─ 30→100Hz插值、限位、hold及Split硬件下发
             │ HTTP multipart；一次最多一个请求
             ▼
GPU 推理服务器
  ├─ g2_sort.training.dm05_lora inference
  ├─ checkpoint + norm_stats.json
  ├─ 补黑边、resize 448×448、normalize
  └─ 10 diffusion steps → 50×34 absolute action
             │
             └─ checkpoint只在这里指定
```

笔记本只是运维入口。正式运行的 client 进程应在 G2 域控制器上，因为相机、
关节状态和硬件控制都是机器人本地资源。client 不访问 checkpoint 文件，
也不需要挂载服务器的`server/`目录；它只配置
正式Sync使用`ws://192.0.2.1:7892/process_frame`的native-image binary WebSocket；
7892 HTTP只保留为首测回退。

## 3. 代码边界

入口`g2_sort/deployment/g2_sdk_client.py`只有启动转发。实现位于
`g2_sort/deployment/g2_client/`：

| 模块 | 唯一职责 |
|---|---|
| `contracts.py` | Observation、ActionChunk、34D/50步/30Hz冻结合同 |
| `policy.py` | 可插拔`PolicyBackend`协议 |
| `dm05_backend.py` | multipart HTTP、JPEG编码、响应严格解析 |
| `cameras.py` | 三路GDK图像解码为RGB，不补黑图 |
| `robot_state.py` | 按名字读取完整34D measured state，不补0 |
| `joint_layout.py` | 34D G2关节名和物理位置限位 |
| `robot_control.py` | Split：14D双臂100Hz Servo、20D双手可配置频率move_ee_pos |
| `hand_command_store.py` | 原子保存最后成功的20D O10命令，供退出后Home复位 |
| `interpolation.py` | 30Hz waypoint的连续时间线性采样 |
| `servo.py` | 独立100Hz streamer、hold和chunk播放 |
| `prefetch.py` | 最多一个异步请求在途 |
| `control_loop.py` | shadow、rtc-shadow、powered-sync、powered-rtc、powered-async状态机 |
| `rtc.py` | RTC的`d/s/H`约束、承诺前缀和真实延迟步数合同 |
| `process_pipeline.py` | spawn策略worker、独立GDK/Camera/JPEG/HTTP和有界IPC合同 |
| `telemetry.py` | request/RTT、原始动作、hold原因和实际双臂/双手下发汇总 |
| `cli.py`、`app.py` | CLI、初始化、信号和清理 |
| `mock.py`、`keyboard.py`、`cleanup.py` | 显式mock、软件急停和有界清理 |

未来接入另一个模型时，只新增一个实现`PolicyBackend.infer(observation)`的后端，
返回相同`ActionChunk`。不得把相机、GDK、servo或调度复制进模型后端。

纯逻辑包导入不依赖`agibot_gdk`；只有创建真实 camera/robot adapter 时才延迟
导入 GDK，因此 GPU 服务器可以直接跑单元测试。

## 4. DM0.5 HTTP 合同

默认 endpoint：

```text
http://192.0.2.1:7892/process_frame
```

请求是`multipart/form-data`：

1. `image=head.jpg`，RGB头图编码后的JPEG；
2. `image=left_wrist.jpg`，RGB左腕图编码后的JPEG；
3. `image=right_wrist.jpg`，RGB右腕图编码后的JPEG；
4. `states`：长度严格为34的JSON数组；
5. `text`：直接引用`g2_sort.config.dm05_contract.PROMPT`。

三个文件字段故意都叫`image`，服务器通过`request.files.getlist("image")`按上传
顺序映射到`images_head`、`images_left_wrist`、`images_right_wrist`。

客户端不 resize 到224或448，不做 CHW/msgpack 转换。服务器沿用训练一致的
保持宽高比补黑边和448×448预处理。

成功响应只能是：

```json
{"response": [[0.0, "... 34 values ..."], "... exactly 50 rows ..."]}
```

缺少`response`、非JSON、空数组、不是`50×34`或包含NaN/Inf都会成为
`PolicyError`，powered模式立即保持安全目标。

## 5. 三种不同的时钟

这三个频率不能混为一个“机器人频率”：

| 时钟 | 冻结/实测值 | 含义 |
|---|---:|---|
| 模型 waypoint | 30Hz | 训练数据中相邻 action 的时间语义 |
| G2双臂Servo | 100Hz | 14D双臂`joint_servo_control`的本地周期 |
| G2 O10双手 | 正式Sync raw 5Hz changed-only；其他模式独立配置 | 20D双手`move_ee_pos`只提交变化目标；模型30Hz轨迹与34D合同不变 |
| 推理请求 | 非固定 | 由服务器RTT、sync前缀或async lookahead决定 |

一个`50×34`chunk表示50个连续的`1/30s`控制区间，总覆盖时间
`50/30≈1.667s`。相邻 waypoint 首点到末点相差`49/30≈1.633s`；最后一个
waypoint在余下的最后一个`1/30s`区间保持。

### 5.1 30→100Hz不是固定复制

在本地 servo 时间`t`计算：

```text
u = t × 30
i = floor(u)
alpha = u - i
target(t) = (1-alpha) × action[i] + alpha × action[i+1]
```

例子：

| servo时间 | `u` | 本地目标 |
|---:|---:|---|
| 0ms | 0.0 | `A0` |
| 10ms | 0.3 | `0.7 A0 + 0.3 A1` |
| 20ms | 0.6 | `0.4 A0 + 0.6 A1` |
| 30ms | 0.9 | `0.1 A0 + 0.9 A1` |
| 40ms | 1.2 | `0.8 A1 + 0.2 A2` |

因此双臂平均是约3.33个Servo tick/模型区间，而不是固定factor 2或每点复制3次。
每个中间目标都是相邻绝对位置的凸组合，不会产生位置过冲。模型 waypoint、
顺序和1.667s总时长不变，所以没有通过改节拍牺牲模型轨迹质量。

### 5.2 硬件输出模式不是模型时间轴

`--servo-output-mode`只改变34D目标如何交给GDK，不改变模型30Hz时间语义：

| 模式 | 双臂 | O10双手 | 用途 |
|---|---|---|---|
| `split` | 14D、100Hz Servo | 20D、正式Sync raw 5Hz changed-only `move_ee_pos` | **正式默认；先运行五秒确认** |
| `arms-only` | 14D、100Hz Servo | 完全不发送 | 隔离O10解析与抖动 |
| `unified-34` | 100Hz Servo | 拼进同一个Servo请求 | 历史故障诊断，不用于日常 |

模型和日志仍保持34D合同，但硬件层不把34D重新拼成Servo请求。`split`线程只采样
最新的插值手部目标，双手调用阻塞不会直接卡住双臂100Hz线程；异步手部故障会在
下一Servo tick传回主控制链并停止。统一34D在当前G2上曾出现O10参数解析失败和
2.53秒阻塞，因此只保留为显式诊断选项。

## 6. 三阶段运行模式

### 6.1 Shadow：默认

```text
等待健康三图/state
  → HTTP推理
  → 50×34/finite校验
  → 限位统计、首动作跳变、30→100Hz完整插值预演
  → telemetry
  → 下一次串行请求
```

shadow不会创建`ServoStreamer`，也不会把servo callback交给状态机；不是简单的
“把速度设为0”。即使代码路径出错，也没有发送动作的对象。`--safe-test`只是
此模式的兼容别名，与任一powered模式同时使用会在参数解析阶段报错。

### 6.2 Powered-sync：当前正式基线

```text
启动Servo线程，但首个安全chunk前发送次数为0
  → 采集健康观测
  → 等待HTTP；独立streamer继续100Hz重复hold
  → 校验响应
  → 重新读取当前34D state
  → 检查有限性、关节限位、动作起点跳变
  → Split执行当前chunk的配置前缀
  → hold
```

第一次HTTP等待期间尚无经验证的安全命令目标，因此绝不调用Servo。首个
chunk通过合同、限位、首跳和动力学门后，才以当时重新读取的34D state为
接入锚点启动100Hz播放。此后模型在服务器推理时，本地streamer才按100Hz
发送最后已验证的安全目标。`04_powered_guarded_smoke.sh`只执行6个区间用于短动作
门禁；正式`05_powered_sync.sh`默认每个chunk只执行前20步（约0.667秒），然后重新采集并请求下一chunk。

### 6.3 Powered-async：显式启用

1. 先在hold状态同步取得第一个健康chunk；
2. 本地100Hz播放当前chunk；
3. 剩余区间小于等于20且没有请求在途时，采集最新观测并启动唯一预取；
4. 旧chunk继续播放，不被HTTP线程阻塞；
5. 响应到达时计算`ceil((now-observation_at)×30)`，跳过陈旧前缀；
6. 至少剩12个新鲜区间、旧播放ID仍有效且request ID递增，才平滑接入；
7. 任一条件失败立即hold，从held state重新同步观测。

提前请求时间线示例：

```text
当前chunk: A0 ---------------- A29 | A30 -------- A49
                                   ↑ 剩20步，capture B + request
                                   | 旧A继续100Hz播放
response B:                              ↓
B原始:       B0 B1 ... Bk(已陈旧) | Bk+1 -------- B49
接入:                              平滑接到Bk+1；必须还剩≥12步
```

如果A49先结束：

```text
A49 → 立即hold → B晚到 → 按播放ID判定迟到并丢弃
                    → 从held state重新capture并做同步请求
```

禁止根据A的末端速度继续外推，也禁止消费上一次成功请求的旧chunk。

## 7. Observation与失败矩阵

| 失败 | Shadow | Powered-sync | Powered-async |
|---|---|---|---|
| 任一路相机无帧/解码失败/shape或dtype错误 | 记录，等待下一次健康观测 | hold，不发请求 | 清当前chunk并hold |
| 必要关节缺失/重复/无名字/非有限 | 记录，不补0 | hold | 清当前chunk并hold |
| HTTP超时/断网/非2xx | 记录，继续 | hold，不消费旧chunk | 清chunk并hold |
| 响应非JSON/错shape/NaN/Inf | 拒绝并记录 | hold | 清chunk并hold |
| 当前state到执行起点跳变超限 | 只统计 | 拒绝并hold | 拒绝并hold |
| 位置越物理限位 | 记录clip统计 | 本地clip后执行 | 本地clip后执行 |
| request ID乱序 | 拒绝 | 拒绝并hold | 清chunk并hold |
| 旧chunk先耗尽、预取仍在途 | 不适用 | 不适用 | hold；迟到响应丢弃 |
| servo调用异常/非零返回 | 不适用 | streamer故障并停止 | streamer故障并停止 |
| SIGINT/SIGTERM/q | 退出 | 锁住软件动作门并停止 | 锁住软件动作门并停止 |

软件急停是额外命令闸门，不能代替机器人硬件急停。任何powered测试仍必须有
操作员守在硬件急停旁。

成功的Split双手请求持续更新最后命令空间20D，不读取`get_end_state`作为复位起点。
首次双手发送延迟80ms，越过60ms边界融合，避免发送近零开手目标。批准Home来自
人工确认的第9条命令；VLA完全退出后只能从30分钟内最后命令通过短时Split回到该
Home。双手5Hz最多8步到Home后，双臂100Hz默认5秒回H5第0帧14D中位数。

## 8. 启动方式

先确认G2域控制器能够访问GPU服务器。Shadow终端执行：

```
./scripts/01_shadow.sh 100
```

通过Shadow门禁后，第一轮无物体Powered-sync终端执行：

```
./scripts/04_powered_guarded_smoke.sh
```

只有sync验证稳定后才启用async。终端执行：

```
./scripts/05_powered_sync.sh --duration-seconds 45

实验性异步终端执行：

```
./scripts/06_powered_async_experimental.sh --duration-seconds 45
```
```

`--mock`用显式合成RGB和34D状态访问真实HTTP服务，适合无GDK环境的协议诊断。
它不是相机失败的黑图回退。

## 9. 用shadow延迟决定timeout和lookahead

至少100次shadow后读取`run_summary`中的P50/P95/P99。需要测的是完整客户端看到
的端到端RTT，包含：

- G2图像JPEG编码；
- 域控制器到GPU服务器的网络；
- Flask排队；
- 10步diffusion推理；
- JSON传输和客户端解析。

设置原则：

```text
timeout > 正常P99 + 明确安全裕量
lookahead_steps / 30 > 正常P95 + 观测/编码/接入裕量
```

当前75K实测HTTP端到端RTT约P50 0.47s、P95 0.52s。P50是中位数；P95表示
95%的请求不超过该值，是更重要的尾延迟。默认lookahead 20相当于约0.667s，
按P95 0.52s约有0.147s裕量；
如果P95已经0.8s，20步在多数请求下都会来不及，不能靠外推掩盖。应先检查GPU
推理、网络和服务排队，再基于实测提高lookahead。lookahead最大仍受50步chunk
限制，且响应接入时必须保留至少12步新鲜动作。

推理请求没有固定30Hz或50Hz：当前正式sync大约每“0.667s动作播放+RTT”请求一次；async由chunk消耗和lookahead触发。实测async没有明显改善动作效果，因此仍
是实验路径。100Hz只属于本地双臂Servo，HTTP线程从不承担100Hz实时职责。

## 10. 当前75K checkpoint到真机

当前工程部署只保留75K推理容器，默认端口7892；35K容器已停止并移除，checkpoint
文件未删除。checkpoint仍由GPU服务器选择，G2客户端不填写checkpoint路径。

1. 确认`checkpoint-75000`完整落盘，验证adapter、34D密集模块、
   `norm_stats.json`和加载合同；
2. 在GPU服务器启动**单卡**inference进程，`model_config.model_name_or_path`
   指向服务器上的`checkpoint-75000`；
3. 服务器启动日志必须明确显示从checkpoint读取norm stats、action_dim=34、
   chunk_size=50、diffusion_steps=10；
4. 用固定三图/34D state连续请求，确认每次严格返回`50×34`有限值；
5. 域控制器运行100次shadow，保存P50/P95/P99和完整telemetry；
6. 人工核对三图内容、34D逐维方向/单位、首动作跳变和限位统计；
7. 无物体、单请求、6区间powered-sync；
8. 多次sync稳定且网络断开必然hold后，才进入powered-async；
9. 最后按`11_CHECKPOINT_SELECTION_TO_ROBOT.md`进入抓取、交接和分拣。

服务器启动参数的核心形式如下，终端执行：

```
python3 -m g2_sort.training.dm05_lora \
  --task inference \
  --model-config.model-name-or-path \
    /absolute/server/path/checkpoint-75000 \
  --inference-config.port 7892
```

实际容器路径、CUDA设备和attention组合应使用服务器已冻结的运行环境；启动前
先在该容器运行`--help`核对Tyro参数名。checkpoint路径不出现在client命令中。
如果使用H20单卡部署，只改变服务器进程所在GPU和endpoint IP，不改变G2
客户端合同、插值或调度代码，也不需要中断8卡训练。

## 11. 自动测试

无GDK主机终端执行：

```
python3 -m pytest -q tests
```

覆盖HTTP合同、三图顺序、错shape/NaN/Inf、严格state、相机失败、插值端点与
无过冲、CLI模式、shadow零servo、sync hold、async single-flight、陈旧前缀
以及迟到响应丢弃。真实GDK、网络和checkpoint仍必须按分阶段门禁实测；单元测试
不能替代真机shadow。
