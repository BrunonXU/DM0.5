---
doc_type: archive
domain: operations
status: archived
created: 2026-07-30
updated: 2026-08-24
models: [old_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: [../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md]
related: []
---

# 35K Checkpoint HTTP服务到G2 Shadow接入操作手册（历史）

> 35K、旧endpoint和旧网络拓扑已经过时。当前上机只使用G2 operator guide。


更新时间：2026-07-30


## 1. 目标、边界与停止条件

本手册把已经选出的`checkpoint-35000`放在RTX 6000D服务器上提供DM0.5 HTTP
推理服务，然后依次完成：

```text
6000D本机服务验收
    → 笔记本网络和真实推理验收
    → 部署包上传G2域控制器
    → G2真实相机/state shadow 1次、10次、100次
    → 负责人决定是否进入powered-sync
```

必须停止并上报的情况：

- 没有负责人明确分配的空闲GPU；
- checkpoint文件不完整或加载日志报错；
- HTTP返回不是严格`50×34`，或出现NaN/Inf；
- 三路相机顺序、34D顺序或单位没有人工确认；
- shadow期间机器人发生任何运动；
- 相机/state错误、网络错误或响应乱序不能解释；
- 准备进入powered但现场没有负责人和硬件急停操作员。

## 2. 两个常用概念

### 2.1 Servo是什么

Servo在本项目里不是“某一个电机”，而是机器人本地的实时目标下发层：

```text
DM0.5给出30Hz absolute joint-position waypoint
       ↓
Client按时间线性插值
       ↓
ServoStreamer每10ms生成一个34D目标
       ↓
GDK joint_servo_control（100Hz）
```

模型等待HTTP响应时，ServoStreamer仍以100Hz重复最后安全目标，这叫hold。模型
线程不承担100Hz实时控制。Shadow模式根本不创建ServoStreamer，所以不会发送
动作。

### 2.2 Mock是什么

Mock是无GDK、无机器人时使用的显式测试输入：

- 自动生成三张可区分的RGB测试图；
- 使用34D测试state；
- 真实执行JPEG、HTTP、服务器推理、响应解析和telemetry；
- 不连接G2相机、关节或执行器。

Mock能证明“网络和输入输出协议跑通”，不能证明：

- 真实相机顺序正确；
- 真实34D关节顺序和单位正确；
- 模型在真实场景中有效；
- 机器人动作安全。

## 3. 参数主次

### 3.1 每次运行基本都会用

| 参数 | 含义 | 常用值 |
|---|---|---|
| `--mode` | 当前阶段 | `shadow`、`powered-sync`、`powered-async` |
| `--policy-endpoint` | 6000D推理地址 | `http://SERVER_IP:7891/process_frame` |
| `--max-requests` | 本次最多请求数 | 调试1、复测10、验收100；0为持续 |
| `--telemetry-jsonl` | 验收记录 | 每次使用不同文件名 |

首先只需要会这四个参数。

### 3.2 进入对应阶段才使用

| 参数 | 使用阶段 | 初始值 |
|---|---|---:|
| `--request-timeout-seconds` | 网络较慢或做延迟门禁 | 10秒 |
| `--execute-prefix-steps` | powered-sync | 6个30Hz区间，约0.2秒 |
| `--chunk-prefetch-lookahead` | powered-async | 20个30Hz区间 |
| `--async-min-fresh-steps` | powered-async | 12 |
| `--interactive-keys` | powered现场 | 开启q+Enter软件急停 |

### 3.3 默认不要改

- `--servo-control-period 0.01`：冻结为100Hz；
- `--max-start-jump-radians 0.35`：动作起点安全门；
- `--boundary-blend-seconds 0.06`：chunk接入平滑；
- `--enable-low-latency-servo`：默认关闭，初期不要开启；
- `--policy-backend dm05`：第一版唯一后端；
- 34D、50步、固定prompt、30Hz：不提供运行时覆盖。

## 4. 角色和变量

| 角色 | 职责 |
|---|---|
| RTX 6000D服务器 | 加载35K、norm stats并监听7891 |
| 笔记本 | ping/端口/mock真实推理验收、上传部署包 |
| G2域控制器 | 运行真实相机/state Client和本地100Hz控制 |

6000D在2026-07-30读到的实验网地址是`192.0.2.1`，但地址可能变化。每次
必须先在服务器执行：

```bash
hostname -I
```

下文统一使用：

```bash
SERVER_IP=192.0.2.1
PORT=7891
```

如实测地址不同，替换`SERVER_IP`。`192.0.2.41`是Docker网桥，不提供给笔记本
或G2使用。

HTTP服务没有身份认证，只允许在受控实验室网络访问。不得把7891直接暴露公网。



---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
-----------------------------------------------------------------------------------------------
----------------------------------XZA负责开ckpt模型端口------------------------------------------
-----------------------------------------------------------------------------------------------
-----------------------------------------------------------------------------------------------
-----------------------------------------------------------------------------------------------
-----------------------------------------------------------------------------------------------


## 5. 阶段A：6000D启动35K HTTP服务

### A0. 负责人分配空闲GPU

### A1. 检查35K结构

服务器项目根目录：

成功标准：

```text
global_step: 35000
state mean/std/q01/q99均为34维
action mean/std/q01/q99均为34维
```

### A2. 在冻结Docker环境启动

先进入一个新的tmux，避免SSH断开带走前台日志：

```bash
tmux new -s dm05-http-35k
```

在tmux中设置负责人分配的GPU，例如：

```bash
PROJECT_ROOT=/home/user/projects/DM0.5
GPU_ID=0
IMAGE='docker.1ms.run/dexmal/opendm@sha256:REDACTED_HOST_FINGERPRINT'
mkdir -p "${PROJECT_ROOT}/server/runs/deployment"
```

启动：

```bash
sg docker -c "docker run --rm \
  --name dm05-g2-http-35k \
  --gpus device=${GPU_ID} \
  --shm-size=16g \
  -e CUDA_VISIBLE_DEVICES=0 \
  -e PYTHONPATH=/workspace:/workspace/opendm \
  -v ${PROJECT_ROOT}:/workspace \
  -w /workspace \
  -p 7891:7891 \
  ${IMAGE} \
  bash -lc 'source /opt/conda/etc/profile.d/conda.sh && \
    conda activate opendm && \
    python g2_sort/training/dm05_lora.py \
      --task inference \
      --model-config.model-name-or-path \
        /workspace/server/checkpoints/dm05_g2_8gpu_50k/checkpoint-35000 \
      --model-config.vision-attn-implementation flash_attention_2 \
      --model-config.action-attn-implementation sdpa \
      --inference-config.port 7891'" \
  2>&1 | tee "${PROJECT_ROOT}/server/runs/deployment/checkpoint-35000_http.log"
```

模型加载完成后用`Ctrl-b d`退出tmux显示，不要按`Ctrl-C`。

成功标准：

- 日志出现`Loading model from ... checkpoint-35000`；
- 日志出现`Using normalization stats from checkpoint`；
- 日志出现`Model loaded successfully`；
- Flask监听`0.0.0.0:7891`；
- 没有CUDA OOM、缺权重、action dimension或norm stats错误。

### A3. 服务器本机端口验收

另开终端执行：

```bash
sg docker -c "docker ps --filter name=dm05-g2-http-35k"
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:7891/process_frame
```

成功标准：

- 容器状态是`Up`；
- GET返回`405`。这只证明路由存在，因为该接口只接受POST；
- GPU上只出现负责人预期的一个推理进程。


## 6. 阶段B：笔记本验收网络和真实推理

### B1. 网络层

在笔记本执行：

```bash
SERVER_IP=192.0.2.1

ping -c 4 "${SERVER_IP}"
nc -vz "${SERVER_IP}" 7891
curl -sS -o /dev/null -w '%{http_code}\n' \
  "http://${SERVER_IP}:7891/process_frame"
```

成功标准：

- `nc`明确显示7891连接成功；
- `curl`返回405；
- ping成功是辅助证据。部分网络禁止ICMP，因此ping失败但TCP成功不算失败；
- TCP失败时先检查IP、路由和实验室防火墙，不修改Client。

### B2. 下载并准备部署包

如果笔记本还没有代码：

```bash
rsync -avP \
  user@"${SERVER_IP}":/home/user/projects/DM0.5/g2_dm05_deployment_20260730.tar.gz \
  /home/user/桌面/

mkdir -p /home/user/桌面/g2_dm05_deployment
tar -xzf /home/user/桌面/g2_dm05_deployment_20260730.tar.gz \
  -C /home/user/桌面/g2_dm05_deployment

cd /home/user/桌面/g2_dm05_deployment
python3 -m venv .venv
source .venv/bin/activate
pip install numpy requests pillow
```

压缩包SHA256：

```text
dfd65c3801b0db55f50949fd712ae428b3f9d767f797553ce8440f9a57ea1376
```

### B3. Mock真实HTTP推理

```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mock \
  --mode shadow \
  --policy-endpoint "http://${SERVER_IP}:7891/process_frame" \
  --request-timeout-seconds 30 \
  --max-requests 3 \
  --telemetry-jsonl laptop_mock_35k.jsonl
```

验收telemetry：

```bash
python3 - <<'PY'
import json
from pathlib import Path

records = [
    json.loads(line)
    for line in Path("laptop_mock_35k.jsonl").read_text().splitlines()
]
chunks = [record for record in records if record["event"] == "action_chunk"]
assert len(chunks) == 3, len(chunks)
assert all(record["shape"] == [50, 34] for record in chunks)
assert not [
    record for record in records
    if record["event"] in {
        "observation_invalid",
        "policy_failure",
        "response_out_of_order",
    }
]
print("PASS: 3/3 responses are finite 50x34")
PY
```

成功标准：

- 三次都产生`action_chunk`；
- shape全部为`[50, 34]`；
- 无合同、HTTP或乱序错误；
- 服务器日志每次显示一次推理和处理耗时；
- 这一步不评价mock动作是否“像抓瓶”。

## 7. 阶段C：部署到G2域控制器

### C1. 上传

在笔记本执行：

```bash
G2_USER=替换成域控制器用户名
G2_IP=替换成域控制器IP

rsync -avP \
  /home/user/桌面/g2_dm05_deployment_20260730.tar.gz \
  "${G2_USER}@${G2_IP}:/home/user{G2_USER}/"
```

登录并解压：

```bash
ssh "${G2_USER}@${G2_IP}"

G2_USER="$(whoami)"
mkdir -p "/home/user{G2_USER}/g2_dm05_deployment"
tar -xzf "/home/user{G2_USER}/g2_dm05_deployment_20260730.tar.gz" \
  -C "/home/user{G2_USER}/g2_dm05_deployment"
cd "/home/user{G2_USER}/g2_dm05_deployment"
```

### C2. 域控制器依赖和网络

```bash
python3 - <<'PY'
import agibot_gdk
import cv2
import numpy
import requests
from PIL import Image
print("PASS: GDK/client dependencies import")
PY

nc -vz 192.0.2.1 7891
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://192.0.2.1:7891/process_frame
```

成功标准：

- 五个依赖都能导入；
- G2域控制器到6000D的7891 TCP连通；
- GET返回405；
- 不通过临时关闭安全策略或把端口暴露公网来“解决”网络。

## 8. 阶段D：G2真实Shadow

开始前：

- 机器人现场有人；
- 操作员知道硬件急停位置；
- 人工预览并确认头、左腕、右腕三路画面没有接反；
- 机器人保持上电但Client必须是shadow；
- 命令中逐字确认存在`--mode shadow`。

### D1. 单次冒烟

```bash
mkdir -p logs

python3 -m g2_sort.deployment.g2_sdk_client \
  --mode shadow \
  --policy-endpoint http://192.0.2.1:7891/process_frame \
  --request-timeout-seconds 30 \
  --max-requests 1 \
  --telemetry-jsonl logs/g2_35k_shadow_1.jsonl
```

成功标准：

- 机器人完全不动；
- 一次健康Observation；
- 一次严格`50×34`响应；
- 无相机/state/HTTP/shape错误。

### D2. 10次稳定性

```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mode shadow \
  --policy-endpoint http://192.0.2.1:7891/process_frame \
  --request-timeout-seconds 30 \
  --max-requests 10 \
  --telemetry-jsonl logs/g2_35k_shadow_10.jsonl
```

成功标准：

- 机器人仍完全不动；
- 10/10 action chunk；
- request ID连续；
- 无黑图回退、无state补0、无乱序或非有限动作；
- 负责人抽查首动作跳变和限位统计。

### D3. 100次正式Shadow

```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mode shadow \
  --policy-endpoint http://192.0.2.1:7891/process_frame \
  --request-timeout-seconds 30 \
  --max-requests 100 \
  --telemetry-jsonl logs/g2_35k_shadow_100.jsonl
```

汇总：

```bash
python3 - <<'PY'
import json
from collections import Counter
from pathlib import Path

path = Path("logs/g2_35k_shadow_100.jsonl")
records = [json.loads(line) for line in path.read_text().splitlines()]
counts = Counter(record["event"] for record in records)
chunks = [record for record in records if record["event"] == "action_chunk"]
summary = [record for record in records if record["event"] == "run_summary"][-1]

print("event counts:", dict(counts))
print("action chunks:", len(chunks))
print("latency:", summary["latency"])
print("max start jump:", max(record["max_start_jump"] for record in chunks))
print("total limit clips:", sum(record["limit_clip_count"] for record in chunks))
PY
```

成功标准：

- `action chunks: 100`；
- `run_summary.latency.count == 100`；
- 零`observation_invalid`、`policy_failure`、`response_out_of_order`；
- 所有shape为`50×34`，后端已经保证finite；
- 机器人全过程零运动；
- 保存P50/P95/P99、最大首动作跳变和限位总数；
- 人工签字确认图像顺序、34D方向和单位。

## 9. 阶段E：是否进入Powered-sync


```bash
python3 -m g2_sort.deployment.g2_sdk_client \
  --mode powered-sync \
  --policy-endpoint http://192.0.2.1:7891/process_frame \
  --request-timeout-seconds 30 \
  --execute-prefix-steps 6 \
  --max-requests 1 \
  --interactive-keys \
  --telemetry-jsonl logs/g2_35k_powered_sync_1.jsonl
```

Powered-sync成功标准：

- 现场负责人和硬件急停操作员均在场；
- 无物体、清空工作空间；
- 只执行一次、约0.2秒模型前缀；
- HTTP等待期机器人hold；
- 动作方向、单位和幅度符合预期；
- 执行结束立即hold；
- 没有start jump拒绝、servo错误或异常限位。

不得自行进入`powered-async`。Async必须在多轮sync稳定和网络断开hold
验证后，由负责人按文档12另行批准。

## 10. 交付记录

每次执行至少填写：

| 字段 | 记录 |
|---|---|
| 日期/操作人 | |
| 服务器IP | |
| GPU ID/负责人 | |
| checkpoint | `checkpoint-35000` |
| Docker镜像digest | `sha256:60a754dce047...` |
| 笔记本mock结果 | |
| G2 shadow 1/10/100 | |
| P50/P95/P99 | |
| 最大首动作跳变 | |
| 限位总数 | |
| 相机顺序人工确认人 | |
| 34D方向/单位确认人 | |
| telemetry路径 | |

任何一步失败都保留日志和telemetry，从失败阶段修复后重做；不得跳过失败步骤进入
下一阶段。
