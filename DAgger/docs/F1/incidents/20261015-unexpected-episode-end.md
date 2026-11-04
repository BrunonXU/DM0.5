# 2026-10-15 示教意外收尾及完成提示音误导

## 现场证据

只读取最近三条事件、journal/payload manifest及所属进程控制台，未运行链路负载测试、发动作或修改现役。时间为北京时间；版本为`20261014-f1-hil-v30-workbench-home`，进程`f1_hil_20261015T025608Z_afd58025`。

| 记录 | 打开至提交完成 | 三图组数 | 结束原因 |
|---|---|---:|---|
| `f1_hil_20261015T025710Z_d9889ae9` | 10:57:10.976–10:57:24.430 | 397 | `operator_watchdog` |
| `f1_hil_20261015T025732Z_7072627a` | 10:57:32.638–10:57:34.250 | 44 | `operator_watchdog` |
| `f1_hil_20261015T025736Z_b68081d2` | 10:57:36.745–10:58:16.384 | 1184 | `start_button` |

前两条分别出现`safe_hold_entered`→`demonstration_stopped(reason=operator_watchdog)`。控制台首次保护状态输入年龄分别217.7ms和211.3ms；现有watchdog为200ms。事件最后一次已处理operator_packet至停止约202.35ms和198.98ms：事件处理时间与网络接收时间不同，不能用后一数值推翻接收年龄判定。全程同一sender session；第一条START计数始终1，第二条始终2，无新的START边沿。不是重复START结束了这两条。

第三条最后收到sequence4224、`hil_toggle_id=4`、`hil_button_pressed=true`，此前计数3；随后`hil_to_handoff_hold(reason=start_button_toggle)`及正常收尾。可证明sender发来了新START，不能只凭机器人日志证明操作者是否有意按键。其Y从monotonic4355486458440至4364400650250完成上抬、右移、回程，约8.914秒；79.7ms后才处理结束START。此条没有Home失败记录。稍后的watchdog出现在已保存之后，不是其结束原因。

三条journal/payload均`complete=true`、`status=completed`、writer_error为空；完成摘要training_ready=false。这里只核对了提交与触发原因，没有重新审核所有图像/动作或认证训练质量。前两条是中断前保存的部分任务，不能视为三次完整成功示教。

## 因果链和代码缺陷

`ChunkPolicy.tick()`发现输入超时后调用`F1PolicyEngine.disconnect()`，后者立即调用`episodes.finish()`，因此保护暂停也结束当前录制。随后操作者用新START恢复人工，会打开下一条记录；普通恢复包不会自动开始。这解释了DATA中的新增条目数量。

`F1PolicyEngine.disconnect()`仅对`camera_unavailable:`传入`interrupted=True`。输入超时/断连仍使用默认false，`DemonstrationRecorder._work()`保存后便调用`audio.notify('saved')`，且未写入中断termination标记。因此正常结束和输入中断保存听起来完全一样。此次原始事件保留了真实停止原因，manifest却没有区分任务中断与普通完成；complete表示写盘提交完整，不代表任务成功。

v30仅改Home配置/规划/相关脚本，没有改START、录制或音频。这是之前未解决的[短时输入间断](20261013-input-latency.md)再次发生，加上已有收尾/提示语义缺陷，并非此次Y复位失败。

## 后续改进与边界

- 将保护中断保存与正常START结束区分：保留原始前缀、标记中断原因、禁止播放成功完成音，页面明确提示。
- 若要短暂停顿不切成新记录，需要独立的录制暂停状态和数据有效段边界；控制仍立即保持，恢复必须有新鲜输入和明确重新授权，不把网络恢复当成自动运动许可。此方案尚未实施。
- 输入延迟来源仍未知，需要Windows采样/发送、隧道、F1接收/调度的同轮证据；仅凭此次日志不能定责Wi-Fi，也不能认定相机、视频处理或磁盘造成超时。继续遵守用户此前暂停主动链路测试的要求，不扩大watchdog。

## 证据保存

原始记录在`/home/user/Bruno/data/hil_data/demonstration/`。开发机私有`DAgger/artifacts/results/20261015-unexpected-episode-end/{collect.py,evidence.json}`保留三条完整events、两个manifest、文件SHA256及本进程控制台尾段；未复制图像载荷、未修改机器人原始数据。此次只完成诊断及文档，未发布修复。
