# 2026-09-08 15:00 存储与GPU只读核对

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](../RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

本轮没有删除文件、停止容器或释放GPU。单位GiB=2^30字节；GB=10^9字节。du为文件实际块占用，df为整个文件系统使用量；不能把du可读子目录当成全盘使用量。

## 6000D / projects

宿主机hq；projects 29.03GiB，占系统盘已用约0.91%。系统盘3.4TiB、已用3.2TiB、剩109GiB（df可用值实际约108.3GiB），97%。本人home约37GiB。

|目录|GiB|projects占比|
|---|---:|---:|
|PI0.5|12.48|43.0%|
|.git|8.38|28.9%|
|g2_robot|2.21|7.6%|
|DAgger|1.94|6.7%|
|tasks|1.64|5.7%|
|DM0.5|1.46|5.0%|
|G2Teleop|0.72|2.5%|
|third_party|0.16|0.5%|
|RLinf|0.03|0.1%|

PI0.5主要为12GiB pi05_base；g2_robot主要为2.3GiB日志；tasks主要为1.7GiB两轮book数据；DM0.5内约1.14GiB旧zip；DAgger包含141409完整副本及历史记录。
.git共8.38GiB，其中git count-objects认定8个tmp_pack垃圾约4.33GiB，mtime为8月4–5日；其余真实Git对象约4GiB，不能当缓存全删。

## 训练盘 /mnt/dm05_nvme

3.5TiB、df已用1.7TiB、可用1.6TiB；可读dm05目录671.39GiB，约占全盘已用39.4%。另有无读权限用户目录，未越权读取，不能把其内容/用途归属给本项目。旧文档说DM0.5/server bind mount，目前实测该目录不是训练盘入口，应以/mnt/dm05_nvme为准。

|dm05子目录|GiB|用途|
|---|---:|---|
|raw_archives|428.17|原始归档，未证实存在可恢复副本，不建议直接删|
|staged_episodes|97.99|解包中间数据，可重建但须核对当前任务引用|
|checkpoints|76.93|训练模型及实验checkpoint|
|processed|56.82|训练处理数据|
|model_cache|10.97|模型缓存，需保留运行依赖|
|其余|约0.51|runs/monitoring/evaluations|

checkpoint候选：smoke_1ep约12GiB、benchmark约4.3GiB；正式75k目录约28GiB、50k目录19GiB、50k→100k目录15GiB。只能筛选无引用试验和中间checkpoint，当前部署75k及选型证据应保留。
Docker报告：Images263.2GB/可回收63.7GB；Containers12.47GB/可回收12.45GB；Volumes7.291GB/可回收2.517GB；Build Cache168.9GB/可回收6.471GB。共享层可能重叠，不能简单相加当保证释放量。未执行prune，未核对每个停止容器是否仍需保留。

## G2

系统overlay26GiB、已用23GiB、可用2GiB，92%；/data约1.5TiB，已用66GiB，可用约1.4TiB。/shared/head、pad各93GiB可用，tmp约186GiB可用，均不自动作为本项目录制目录。

|用户项目实际占用（两卷合并）|GiB|三项目占比|
|---|---:|---:|
|DAgger（含/data链接目标）|7.33|67.6%|
|DM0.5|2.85|26.3%|
|G2Teleop|0.66|6.1%|

DAgger代码在/home/agi/xza/DAgger约22MiB，data/logs通过符号链接落在/data/logs/xza/DAgger；DM0.5约2.9GiB几乎都是旧日志；G2Teleop约675MiB，其中日志506MiB。PI0.5/RLinf是4KiB占位目录。
/data可读分布：.cache约23GiB、record20GiB、ota15GiB、logs9.8GiB（含DAgger7.3GiB）、dlb635MiB。前几项包含机器人系统/其他任务，未证实可删；不应一键清理。

## 三轮记录清理建议

140711/141036/141409三轮含遥测合计4.464GB；payload合计4.058GB。建议保留141409完整原始数据作回归基线，前两轮如不再复查可仅清理payload约2.704GB，保留journal/manifest/审计。6000D目前只有最后141409的完整新轮副本；前两轮未复制完整原图，删除会失去其图像证据。拼接MP4约8.6MB是有损缩放展示，不是原图备份。大盘尚有1.4TiB，当前无迫切删除必要。

## GPU最终快照（15:00:23 CST）

|GPU|显存MiB / 85651|利用率|归属依据|
|---|---:|---:|---|
|0|809|0%|远程桌面及其他用户仿真进程|
|1|73121|0%|其他用户进程|
|2|3815|9%|其他用户仿真进程|
|3|12130|0%|本项目RTC/7893，主PID12237，另有其他用户少量占用|
|4|73124|0%|其他用户进程|
|5|17851|0%|其他用户进程|
|6|35347|0%|其他用户进程|
|7|12562|0%|本项目Sync/7892，主PID12236，另有其他用户少量占用|

GPU3/7归属通过docker top与nvidia-smi PID交叉验证：NewLora-dm05-ws-75k-rtc / NewLora-dm05-ws-75k-formal。0%为瞬时利用率，不表示显存可直接占用或进程可终止。最初沙箱内nvidia-smi失败，宿主权限只读检查正常，不是GPU故障。
