# 2026-09-08 启动失败：系统盘ENOSPC

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

直接读取G2最新日志确认：135125轮连接后recording writer failed；135317轮创建journal.partial时明确报Errno 28 No space left on device。df显示overlay系统盘26G、可用0、100%；inode仅11%，不是inode耗尽。检查时无owner锁。本轮未启动真机。

已将本项目data与logs复制到可写的大容量卷/data/logs/xza/DAgger，逐文件SHA-256与大小完全一致后才移除旧重复副本，并将原路径替换为符号链接：

- data：16113文件，2151327818字节；包括历史与.partial失败记录，未删除episode。
- logs：16文件，77051151字节。
- 迁移后系统盘可用2159341568字节；/data原有约1.4TB可用。

用户仍从/home/user/xza/DAgger运行，无需更改Windows。只迁移本项目data/logs，未改系统服务及其他项目。/data/logs/xza/DAgger包含需要保留的实验原始数据，不是可随意清理的缓存；尚未核对系统是否有通用日志保留策略。

补充文件预检：解析实际目标卷，data/logs/telemetry要求2GiB启动余量，/tmp要求256MiB。该数值是存储余量，不能保证任意时长录制都装得下；运行中写盘失败仍停止候选。writer异常现在保留payload/journal具体错误，不再只显示笼统recording writer failed。

新增测试覆盖满盘和符号链接目标卷。局部离线10 passed。全套与G2部署验证见后续补充。真机重启和30Hz效果待现场验收。

部署验证：本地全套56 passed（2.06s），G2全套56 passed（3.95s）；G2 files-only预检通过，未导入或初始化GDK。修复代码已同步。
