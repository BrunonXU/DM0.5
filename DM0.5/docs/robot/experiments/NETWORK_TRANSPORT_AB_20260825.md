---
doc_type: experiment
domain: transport
status: active
created: 2026-08-25
updated: 2026-08-26
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [newlora_transport_n0_n3_ab_20260825]
supersedes: []
superseded_by: []
related: [../TRANSPORT_BINARY_WEBSOCKET.md, PROCESS_ISOLATION_AND_TRANSPORT_20260824.md]
---

# NewLora图片与网络传输N0-N3实验

## 1. 假设与唯一变量

本实验验证两个独立假设：原生GDK JPEG透传能删除G2的decode/re-encode并减少请求字节；
持久binary WebSocket能删除multipart/JSON动作列表开销。N0/N1只改变图片字节，N1/N3
只改变传输协议。未将模型、去噪步数、compile、checkpoint或action合同作为变量。

## 2. 冻结条件

- checkpoint：NewLora 75K official augmentation v1；
- 5 diffusion steps、`torch.compile` reduce-overhead、输出固定50×34；
- 6000D同一RTX 6000D GPU1，同一时刻只保留一个待测服务驻留，避免GPU争用；
- 固定归档：`g2_robot/logs/diagnostics/preflight_20260824_144920`；
- current三图396611 bytes，native三图284655 bytes，减少28.23%；
- HTTP与WebSocket分别使用独立7897/7896候选，生产7892/7893未修改；
- 每组先暖机3次；N0/N1/N2为60次，最终N3为100次；以下是6000D host loopback，
  不等价于G2到6000D真实网络。

## 3. 结果

| ID | 图片 | 协议 | n | mean | P50 | P95 | P99 | max |
|---|---|---|---:|---:|---:|---:|---:|---:|
| N0 | current JPEG | HTTP multipart/JSON | 60 | 164.39ms | 163.04ms | 171.11ms | 172.88ms | 174.86ms |
| N1 | native GDK JPEG | HTTP multipart/JSON | 60 | 158.07ms | 157.86ms | 159.65ms | 164.18ms | 165.47ms |
| N2 | current JPEG | binary WebSocket | 60 | 170.65ms | 154.86ms | 167.74ms | 520.48ms | 1009.54ms |
| N3 | native GDK JPEG | binary WebSocket | 100 | 154.55ms | 152.15ms | 163.06ms | 169.39ms | 196.05ms |

N2的一次约1.01秒异常完全落在模型段；服务日志没有recompile、协议错误或断线。随后N3
连续100次未复现模型长尾，模型P50/P95/P99为134.75/138.33/139.81ms。N3最大196.05ms
来自input transform尾部，不是网络或模型重编译。因此不能把N2异常归因于WebSocket，
也不能在更长真实网络Shadow前宣称长尾已经消失。

相对N0，N3 mean/P50/P95分别降低9.84/10.89/8.06ms；P99降低3.49ms。HTTP JSON响应
平均约32.6KB，binary响应约7.8KB。WebSocket请求只比三图payload多199 bytes，compression
关闭。float32动作响应本次量化记录为0；此前协议上界测试低于1.2e-7rad。

## 4. 动作质量门

同一归档、同seed的native与current动作A/B已在2026-08-24完成：native相对current
全动作MAE约0.000400rad，显著小于不同seed约0.004459rad的模型自身离散。该结果允许native
进入Shadow，不等于真机任务成功率已验证。

## 5. 决策与限制

当前Shadow候选选择N3：原生GDK JPEG、persistent binary WebSocket、compression off、
50×34 little-endian float32 response。它在loopback上是mean/P50最优且字节合同最小；N1在
本样本的P95/P99略低于N3，所以WebSocket尾部优势仍需G2真实链路确认，不能直接替换生产。

## 6. G2真实网络Shadow

G2于2026-08-25运行N3 Shadow并在约68.37秒后手动终止，完成348/400个请求。日志明确记录
`client_camera_decode_seconds=0`、`client_jpeg_encode_seconds=0`，三路native GDK JPEG平均
262434 bytes；binary请求/响应平均262633/7773 bytes。没有Servo或dispatch事件。

| 指标 | P50 | P95 | P99 | max |
|---|---:|---:|---:|---:|
| observation→Control | 184.95ms | 211.37ms | 260.69ms | 612.86ms |
| WebSocket roundtrip | 183.16ms | 209.02ms | 258.87ms | 610.70ms |
| transport wire residual | 30.43ms | 48.09ms | 76.35ms | 111.51ms |
| server handler | 152.33ms | 157.17ms | 182.98ms | 583.80ms |
| server model+CPU | 135.82ms | 137.61ms | 139.71ms | 566.64ms |

第47和333个请求分别出现约523/567ms模型段尖峰，占0.57%；服务端无recompile、协议错误或
断线证据。结束时唯一`ConnectionClosedOK`与操作者手动终止同秒，是关闭过程中server发送
响应的race；客户端`policy worker fatal response`同样属于停止清理，不是运行中断线。

与此前五步compile、双进程HTTP真机日志相比，N3 observation→Control P50从约272.50～
278.62ms降到184.95ms，P95从约295.97～312.37ms降到211.37ms，改善约85～101ms。但这不是
同次严格HTTP/N3交替A/B：旧HTTP日志来自Powered Sync/RTC，任务调度与场景不同。分项账本显示
收益主要来自删除G2 decode/JPEG约44ms和binary server non-model路径约57ms；本次WebSocket
wire residual约30ms，反而高于旧HTTP参考约13～14ms。所以下一步应做匹配HTTP Shadow，而非
把全部收益归因于WebSocket。

## 7. 2026-08-26正式迁移

用户决定不再等待匹配HTTP Shadow，先把N3用于正式Sync。7896先作为GPU7候选完成验证，
随后把正式服务恢复到原Sync端口7892；G2`05_powered_sync.sh`默认指向7892。7896停止，
7893/7894停止。RTC不在本轮一起切换，待Sync结果返回后仍以原7893端口在GPU3部署和门禁。

GPU1阶段的7896候选首次compile约20.08秒；无新外部重负载时暖态10次P50/P95/max为
163.4/174.0/178.1ms，模型段P50约137.8ms。迁到GPU7后首次compile约19.73秒，暖态
10次P50/P95/max为158.5/182.3/188.1ms，模型段P50约139.7ms；这仍是6000D本机基准，
不是G2端到端结果。正式进程随后去掉RTC启动参数，变为纯Sync；重新compile约20.77秒，
5次暖态请求P50/P95/max为162.5/202.8/207.3ms，`50×34 float32`合同通过，模型段P50
约142.3ms。需保留两层不同口径：严格同GPU N0→N3
只改善约8–11ms；旧真机HTTP与N3 Shadow的非严格对照改善约85–101ms，主要包括G2
decode/re-encode和server non-model路径，不得把100ms全部归因于WebSocket。

GPU1阶段出现其他用户约9.4GB任务时，7896候选P50/P95退化到260.0/294.0ms；因此正式
Sync的计算迁到GPU7，但保持原7892端口。RTC仍需基于未来GPU3/7893的真实分布重新标定`d/s`。
