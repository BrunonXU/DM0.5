# 本地录制与训练导出

新统一 Pico 与手柄示教 / HIL 将原件保存在机器人本地，原件保存不依赖 MP4 服务器。当前数据页播放已生成的 MP4；视频生成与训练导出是可选的后续流程。

录制、保存、人工审核和训练就绪分别确认。Pico 来源需要匹配的独立上肢控制合同，手柄来源需要匹配的 DAgger 运行端，不能用页面能打开代替真实采集验收。

## 保存在哪里

| 配置 | 作用 |
| --- | --- |
| `runtime/storage.json` | 新统一数据根，初始化默认 `~/F1-data`；归档上传默认关闭 |
| `runtime/recording.json` | 统一 Pico 录制参数；初始化选择 Pico 时生成 |
| `configs/installation.env` 的 `F1_RECORDING_PYTHON` | 与 ROS 兼容的独立录制 Python |
| `runtime/hil.json` 与外部 DAgger 配置 | 手柄原件的写入和读取路径，二者需一致 |

在机器人项目根执行 `bash recording/run.sh --check` 检查录制依赖。该检查不开始采集。实际 Pico 录制由页面明确准备、开始、结束审核和保存；手柄录制按 [手柄说明](HIL.md) 与运行端提示操作。

新统一录制默认 30Hz，保存三路 RGB 原图与来源记录；state 为真实反馈，action 为实际发布目标。16D 合同为双臂各 7 轴加左右夹爪，手臂单位为度，夹爪为 0 开、100 闭。头部和身体等上下文保留为来源信息，不混入 16D 动作。

录制时不编码三路 MP4。本机接收时间配对不等于相机曝光同步，网页预览帧率也不决定采集频率。异常中断原件与正常提交分开保存，不能直接作为已审核训练数据。

## 离线转换与审核

仅需要训练数据时，在独立处理机准备 `recording/unified/lerobot_binding.json` 锁定的 LeRobot 版本和来源适配依赖。以下命令从处理机的 F1-infra 源码根执行，所有 `/path/to/` 路径替换为本机实际目录：

```bash
python -m recording.unified inspect --source /path/to/source
python -m recording.unified review-template --source /path/to/handheld-run \
  --dagger-root /path/to/DAgger --output /path/to/review.json
python -m recording.unified export --source /path/to/handheld-run \
  --dagger-root /path/to/DAgger --lerobot-root /path/to/locked/lerobot \
  --output /path/to/datasets/candidate
```

Pico 来源由 `capture.json` 识别，无需 `--dagger-root`。导出前先用 `inspect` 检查来源和缺项；候选导出不表示人工审核已完成。

审核模板绑定源数据和片段选择，人工填写后使用 `--review /path/to/review.json` 导出到另一个新目录。输出使用固定版本官方 LeRobot v3 writer，并回读核对 state / action、时间轴和三路图像；失败保留 `.partial`，不覆盖原件或伪造缺失动作。

历史来源若不具备完整真实动作与来源证据，检查结果会给出阻塞原因。不同模型的 resize、归一化和表示转换在训练适配器完成，不能把 v3 格式兼容当作模型无需适配。

## 视频与上传

需要网页 MP4 时，按 [可选视频服务器](SHARING.md#可选视频服务器) 配置兼容来源的派生任务。统一 Pico 的视频派生按其来源单独处理，不保证所有录制都由同一 worker 自动生成视频。

归档上传默认关闭。转换任务队列不是自动上传器，源码共享盘也不是数据上传地址。本机日志、原件、审核结果和导出数据不随源码包分发。
