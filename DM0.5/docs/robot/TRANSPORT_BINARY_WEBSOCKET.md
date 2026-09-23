---
doc_type: architecture
domain: transport
status: active
created: 2026-08-24
updated: 2026-08-25
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [g2_dm05_binary_transport, image_transport_tradeoff]
supersedes: []
superseded_by: []
related: [INFERENCE_LATENCY_FIRST_PRINCIPLES.md, RTC_PROCESS_ISOLATION.md, experiments/PROCESS_ISOLATION_AND_TRANSPORT_20260824.md]
---

# G2到DM0.5的binary WebSocket传输

## 1. 为什么要改

五步compile后，约279.5ms E2E中model+CPU约138.9ms，non-model约140.6ms。模型与管线各占约一半，此时继续把模型压18ms，理论收益小于消除一次完整图像decode/re-encode、multipart/JSON和重复连接框架成本。

旧链路是：

```text
GDK JPEG
→ G2 OpenCV decode为RGB
→ Pillow quality95再次JPEG
→ multipart HTTP
→ 服务器Pillow decode
→ PadToSquare + Resize448
→ model
→ 50×34 Python list
→ JSON文本
→ G2 JSON parse + float64 array
```

不可再约简的事实是：相机本来已经给出压缩JPEG。若网络只需要这张图片，把它先恢复为约百万像素数组、再做一次有损JPEG，既增加CPU/内存工作，又改变模型像素；这不是协议必需工作。

## 2. 图像方案不是一个开关

“原始JPEG透传”和“机器人端resize”不能同时被描述为零decode/re-encode。任意448缩放必须知道每个输出像素来自哪些输入像素，因此必须先恢复像素；普通JPEG又不能在任意尺寸上无损缩放，所以之后还需新编码。

当前评估四条路径：

| 候选 | G2工作 | 服务器工作 | 质量/带宽影响 |
|---|---|---|---|
| 旧decode→JPEG95 | decode+有损重编码 | decode+resize | 当前基线；存在第二代JPEG误差 |
| 原始JPEG透传 | 无像素decode、无重编码 | decode+resize | 保留相机原始信息；模型输入相对旧基线有小变化 |
| G2 resize448→PNG | decode+resize+PNG | decode，第二次448 resize应为identity | 模型像素可严格等价raw，但PNG CPU/带宽较重 |
| G2 resize448→JPEG95 | decode+resize+JPEG | decode | 字节最小，但再次有损，模型像素变化最大 |

2026-08-24三张真实预检归档图的结果。注意预检文件是GDK图像解码为RGB后再以JPEG95保存，不是原生GDK压缩字节；本实验精确量化“再增加一代JPEG”的影响，原生payload仍要在下次G2 shadow记录：

- 归档输入JPEG总计约456KB，透传候选额外图像处理为0；
- 旧二次JPEG相对raw最终448像素MAE为0.029～0.050/255，2.7%～4.3%的通道值不同；说明旧链路本来就在轻微改变图像；
- resize448 PNG逐像素等价raw，但三图总计约541KB，且测试环境三图处理约105ms，不值得用延迟换“前移transform”；
- resize448 JPEG只有约122KB，但最终像素MAE为0.69～1.02/255，41%～55%的通道值不同，必须先做固定seed动作A/B。

因此第一候选是原生GDK JPEG透传：从原理上它删除工作最多、避免再增加一代有损误差、也最容易解释。归档实验还不能量化实际GDK payload大小和像素；生产切换前须先记录native payload，并运行`evaluate_image_transport_actions.py`。

## 3. WebSocket为什么有意义

HTTP keep-alive只能复用TCP的一部分，但每轮仍构造multipart边界、字符串form、JSON动作列表并经过Flask请求/响应框架。WebSocket把连接生命周期从“每个请求”提升为“Policy worker生命周期”；compression显式关闭，因为JPEG已经压缩，继续压缩通常只增加CPU和延迟方差。

长连接不直接让GPU更快。它删除的是每轮不随任务语义变化的协议工作，并让传输计时边界更清楚。断线后当前请求直接失败，不自动重发一个结果不确定的推理；下一轮才重建连接。

## 4. binary合同

协议版本由8字节magic冻结。请求包含：

```text
request_id uint64
flags / RTC d / s / prefix_steps
state float32[34]
optional RTC prefix float32[N,34]
3 × {encoding, width, height, byte_length, native JPEG/PNG bytes}
```

prompt、34D顺序和H=50是冻结部署合同，不每轮重复传文本。响应包含：

```text
request_id uint64
status
actions float32[50,34]  # 固定6800 bytes
small JSON timing/error metadata
```

timing继续用小JSON，是因为键集合仍在诊断期演化；真正的大数组全部是little-endian float32。跨G2/DM0.5两份实现用同一个golden SHA256做wire compatibility门，防止某一边单独改字段。

## 5. float32会不会伤害动作质量

服务器模型输出本来来自float/bfloat计算，HTTP服务器最终也把RTC prefix/state转成float32。把动作从JSON decimal→float64数组改为直接float32，不改变50×34语义，只增加一次float32量化。

量化误差的上界必须实测记录为`server_action_float32_quantization_max_abs`。本地覆盖[-2,2]的协议测试最大误差低于1.2e-7rad，远低于编码器、模型随机性、Servo分辨率和当前毫弧度级动作变化。若真实响应出现异常大值，finite/shape/物理限位门仍在Control侧生效。

## 6. 当前状态和门禁

已完成：native JPEG/PNG capture、binary request/response、persistent socket client、WebSocket server入口、compression关闭、单请求锁、断线失败语义、跨仓库golden wire hash、真实localhost两请求单连接测试、6000D独立7896部署、固定seed动作A/B以及N0-N3匹配服务E2E。最终N3的100次loopback mean/P50/P95/P99为154.55/152.15/163.06/169.39ms；详见[`experiments/NETWORK_TRANSPORT_AB_20260825.md`](experiments/NETWORK_TRANSPORT_AB_20260825.md)。

尚未完成：G2三相机真实网络Shadow。旧`http-multipart-json`仍为默认；只有显式选择`websocket-binary`且启用进程隔离才可进入候选路径。N3尚未进入powered Sync或RTC。

RTC `d/s`不能在旧279.5ms数据上提前改。必须先获得新transport的observation→Control响应分布，再把延迟覆盖、H=50可续接窗口和固定seed轨迹质量一起评估。
