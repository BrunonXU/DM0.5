# 2026-10-16 最近录制没有MP4：服务器视频服务认证失配

最近MP4缺失的直接原因是服务器worker仍使用旧访问码，F1已按用户要求统一为1，登录持续返回401。已同步worker私有凭据并仅重启服务器视频服务，队列重新进入传输；不是原始录制丢失。

正常链路仍有明显传输等待。用户随后明确选择继续服务器编码，见[服务器传输优化](20261016-server-preview-transfer.md)；下方机载方案保留为已被替代的讨论记录，没有启用机载编码。

```mermaid
flowchart LR
    A[F1保存原图与日志] --> B[闲时排队传到服务器]
    B --> C[FFmpeg编码三路MP4]
    C --> D[回传F1派生预览]
```

## 诊断与恢复证据

2026-10-16服务器 `f1-09-session-preview.service` 为active，但日志每10秒报告workbench unavailable/session expired。经现有隧道查询/api/session为200，使用配置里的旧token登录/api/login为401（控制码或访问码不正确）；验证用户此前指定的1可成功登录。未打印或导出原凭据。

私有配置原地备份为token文件旁 `.before-20261016`（0600），写入已验证值并重启本用户视频服务。未重启F1、相机、驱动或动作程序。恢复后机器人session_preview队列为8 ready、1 transferring、4 waiting_server，无failed；最新 `f1_hil_20261016T024951Z_b2e7b6ad` / episode `7c7c358880f99079b34cf278` 已入队waiting_server。服务器缓存正在接收源文件，不能将排队恢复写成最新MP4已生成。

现场worker为6000D的 `results/session-preview-server/v28/session_preview_worker.py`，配置和令牌保持服务器本地。原始数据留F1；服务器只缓存转换来源，派生MP4回传到F1的 `runtime/data_previews/<episode-id>`，发布成功后按既有收据清理来源缓存。此流程与LeRobot训练导出队列不同，payload的auto_video=false也不代表session_preview关闭。

## 为什么慢

现行server将HIL整个运行进程active视为忙，scan/claim/transfer暂停，不仅仅是单条recording；保持运行端等待下一条也会延迟视频。完整三路pack与日志经128KiB串行JSON/base64传输，双向限速2MiB/s，worker串行处理任务。

10月13日已有对照记录：某条来源下载约459秒、编码约20秒、后续回传发布约59秒；服务器同缓存独立复测编码22.019秒。这些是历史测量，不是本次最新条耗时。462.9MB量级即使理想达到限速，单原始数据传输也需数分钟，且未计排队与回传。数据页面优先展示原图；是否存在MP4应以preview_status及视频列表判断，不能仅凭截图断言未生成。本次队列已证实最新条尚未生成。

## F1本机编码的建议与限制（后续用户决定不采用）

本轮只读识别 `/proc/device-tree/model` 为NVIDIA Jetson AGX Thor Developer Kit；ffmpeg -encoders列出h264_nvenc、hevc_nvenc、libx264等。没有运行编码或采集并发压测；列出编译能力不代表实际硬编已经验证。[FFmpeg官方说明](https://ffmpeg.org/ffmpeg.html)要求运行时硬件与驱动满足支持条件；[NVIDIA Thor官方FFmpeg说明](https://docs.nvidia.com/jetson/archives/r39.2/DeveloperGuide/SD/Multimedia/AcceleratedDecodeWithFfmpg.html)描述对应版本能力，不能替代本机JetPack/驱动验证。

建议先在空闲时验证机载硬编，读取已提交原始包，单任务、低优先级、有界缓冲，采集开始时让出资源，暂停/失效需可靠握手；未经实现和验收不得声称START立即抢占已保证。三路原始落盘和时间戳不变，预览失败不影响原件，服务器保留重试/批处理。硬编仍需JPEG解码、颜色转换、内存搬运和磁盘读写，不是零开销。

上线并发前应比较采集单独运行与采集加预览的真实三图频率/最大及P99间隔、丢帧、写盘积压、手柄接收至消费延迟，以及CPU/内存/IO/编码器负载。仅看平均30Hz或CPU平均值不够。本轮只恢复既有服务器流程，不改busy门控或机载编码策略。
