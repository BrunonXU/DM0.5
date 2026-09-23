---
doc_type: archive
domain: g2_control
status: superseded
created: 2026-07-29
updated: 2026-08-24
models: [old_lora]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: [../../robot/RTC_PROCESS_ISOLATION.md, ../../../../g2_robot/docs/12_G2_CLIENT_DM05_RUNTIME.md]
related: []
---

# G2 Client → DM0.5 真机运行时（历史副本）

> 本副本早于G2仓库现场版本，且未包含当前单Robot-owner目标架构，禁止作为当前实现依据。

更新时间：2026-07-29

## 1. 文档职责

本文是 G2 客户端运行时、DM0.5 HTTP 合同、30/100Hz 时钟和三阶段真机接入
的唯一详细来源。模型训练合同读`04_TRAINING_DEPLOYMENT.md`，checkpoint选择
门禁读`11_CHECKPOINT_SELECTION_TO_ROBOT.md`。

第一版冻结条件：

- 三路 RGB，顺序为头、左腕、右腕；
- `g2_contract.PROMPT`中的固定英文 prompt；
- 34D measured state；
- `50×34` absolute joint-position action；
- 模型动作时间网格 30Hz；
- G2 本地 `joint_servo_control` 100Hz；
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
  └─ 30→100Hz 插值、限位、hold、servo
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
关节状态和 100Hz servo 都是机器人本地资源。client 不访问 checkpoint 文件，
也不需要挂载服务器的`server/`目录；它只配置
`http://<GPU_SERVER_IP>:7891/process_frame`。

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
| `robot_control.py` | GDK控制模式和统一`joint_servo_control`下发 |
| `interpolation.py` | 30Hz waypoint的连续时间线性采样 |
| `servo.py` | 独立100Hz streamer、hold和chunk播放 |
| `prefetch.py` | 最多一个异步请求在途 |
| `control_loop.py` | shadow、powered-sync、powered-async状态机 |
| `telemetry.py` | request、RTT、动作、跳步、限位和hold原因 |
| `cli.py`、`app.py` | CLI、初始化、信号和清理 |
| `mock.py`、`keyboard.py`、`cleanup.py` | 显式mock、软件急停和有界清理 |
| `rl.py` | 从旧客户端隔离保留的实验性残差RL；正式DM0.5 CLI不启用 |

未来接入另一个模型时，只新增一个实现`PolicyBackend.infer(observation)`的后端，
返回相同`ActionChunk`。不得把相机、GDK、servo或调度复制进模型后端。

纯逻辑包导入不依赖`agibot_gdk`；只有创建真实 camera/robot adapter 时才延迟
导入 GDK，因此 GPU 服务器可以直接跑单元测试。

## 4. DM0.5 HTTP 合同

默认 endpoint：

```text
http://127.0.0.1:7891/process_frame
```

请求是`multipart/form-data`：

1. `image=head.jpg`，RGB头图编码后的JPEG；
2. `image=left_wrist.jpg`，RGB左腕图编码后的JPEG；
3. `image=right_wrist.jpg`，RGB右腕图编码后的JPEG；
4. `states`：长度严格为34的JSON数组；
5. `text`：直接引用`g2_sort.data_tools.g2_contract.PROMPT`。

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
| G2 servo | 100Hz | 域控制器向`joint_servo_control`发送目标的本地周期 |
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

因此平均是约3.33个servo tick/模型区间，而不是固定factor 2或每点复制3次。
每个中间目标都是相邻绝对位置的凸组合，不会产生位置过冲。模型 waypoint、
顺序和1.667s总时长不变，所以没有通过改节拍牺牲模型轨迹质量。

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

### 6.2 Powered-sync：第一轮真机默认powered路径

```text
100Hz hold
  → 采集健康观测
  → 等待HTTP；独立streamer继续100Hz重复hold
  → 校验响应
  → 重新读取当前34D state
  → 检查有限性、关节限位、动作起点跳变
  → 插值执行6个30Hz区间（约0.2s）
  → hold
```

这里没有“机器人等模型时失去100Hz”。模型在服务器推理时，本地streamer仍按
100Hz发送最后安全目标。每个新chunk只执行短前缀再重新观测，不会第一次就
开环执行50步。

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

## 8. 启动方式

以下命令在 G2 域控制器的项目根目录执行。先确认域控制器能够访问GPU服务器：

```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mode shadow \
  --policy-endpoint http://<GPU_SERVER_IP>:7891/process_frame \
  --request-timeout-seconds 10 \
  --max-requests 100 \
  --telemetry-jsonl logs/g2_shadow_100.jsonl
```

通过shadow门禁后，第一轮无物体powered-sync：

```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mode powered-sync \
  --policy-endpoint http://<GPU_SERVER_IP>:7891/process_frame \
  --execute-prefix-steps 6 \
  --max-requests 1 \
  --interactive-keys \
  --telemetry-jsonl logs/g2_powered_sync.jsonl
```

只有sync验证稳定后才启用：

```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mode powered-async \
  --policy-endpoint http://<GPU_SERVER_IP>:7891/process_frame \
  --chunk-prefetch-lookahead 20 \
  --async-min-fresh-steps 12 \
  --interactive-keys \
  --telemetry-jsonl logs/g2_powered_async.jsonl
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

默认lookahead 20相当于约0.667s。如果实测P95是0.45s，它有约0.217s裕量；
如果P95已经0.8s，20步在多数请求下都会来不及，不能靠外推掩盖。应先检查GPU
推理、网络和服务排队，再基于实测提高lookahead。lookahead最大仍受50步chunk
限制，且响应接入时必须保留至少12步新鲜动作。

推理请求没有固定30Hz或50Hz：sync大约每“RTT+0.2s”一次；async由chunk消耗
和lookahead触发。100Hz只属于本地servo，HTTP线程从不承担100Hz实时职责。

## 10. 35K候选checkpoint到真机

35K只是工程接入候选，不自动成为最终模型：

2026-07-29只读核对结果：`server/checkpoints/dm05_g2_8gpu_50k/checkpoint-35000`
已存在，`trainer_state.json.global_step=35000`；包含约649MB的
`adapter_model.safetensors`、`adapter_config.json`、`norm_stats.json`、
optimizer/scheduler和8个rank的RNG。adapter的`modules_to_save`包含34D
`action_in_proj/action_out_proj`及time modules，state/action norm stats各项均
为34维。这证明35K保存结构完整，不等于validation或真机效果已通过。

1. 确认`checkpoint-35000`完整落盘，验证adapter、34D密集模块、
   `norm_stats.json`和加载合同；
2. 在GPU服务器启动**单卡**inference进程，`model_config.model_name_or_path`
   指向服务器上的`checkpoint-35000`；
3. 服务器启动日志必须明确显示从checkpoint读取norm stats、action_dim=34、
   chunk_size=50、diffusion_steps=10；
4. 用固定三图/34D state连续请求，确认每次严格返回`50×34`有限值；
5. 域控制器运行100次shadow，保存P50/P95/P99和完整telemetry；
6. 人工核对三图内容、34D逐维方向/单位、首动作跳变和限位统计；
7. 无物体、单请求、6区间powered-sync；
8. 多次sync稳定且网络断开必然hold后，才进入powered-async；
9. 最后按`11_CHECKPOINT_SELECTION_TO_ROBOT.md`进入抓取、交接和分拣。

服务器启动参数的核心形式是：

```bash
python3 -m g2_sort.training.dm05_lora \
  --task inference \
  --model-config.model-name-or-path \
    /absolute/server/path/checkpoint-35000 \
  --inference-config.port 7891
```

实际容器路径、CUDA设备和attention组合应使用服务器已冻结的运行环境；启动前
先在该容器运行`--help`核对Tyro参数名。checkpoint路径不出现在client命令中。
如果使用H20单卡部署，只改变服务器进程所在GPU和endpoint IP，不改变G2
客户端合同、插值或调度代码，也不需要中断8卡训练。

## 11. 自动测试

无GDK主机执行：

```bash
python3 -m pytest -q g2_sort/tests/test_g2_client_runtime.py
```

覆盖HTTP合同、三图顺序、错shape/NaN/Inf、严格state、相机失败、插值端点与
无过冲、CLI模式、shadow零servo、sync hold、async single-flight、陈旧前缀
以及迟到响应丢弃。真实GDK、网络和checkpoint仍必须按分阶段门禁实测；单元测试
不能替代真机shadow。
