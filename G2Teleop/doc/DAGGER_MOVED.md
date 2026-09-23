# HIL / DAgger 已迁移

最新进度见 [DAgger 2026-09-08核对](../../DAgger/doc/STATUS_20260908.md)，不要将控制运行完成当作训练数据审计通过。

Windows目录缺失需先从G2的DAgger/windows_client下载src和scripts到$HOME/DAgger；sender CMD参数错误已修复为--host 127.0.0.1 --port 7907。统一7907→G2 7906传输可用于12普通遥操，不能因客户端目录名推断为HIL。

2026-09-07启动锁修复见 [DAgger记录](../../DAgger/doc/RUNTIME_LOCK_FIX_20260907.md)：实际是活跃g2_paos bridge占用，40已改为加载GDK前预检拦截；未删除锁或终止进程。

后续父进程核查：bridge在pts/2交互bash下运行，非40遗留子进程；详见同一记录。

来源与功能进一步核查见 [PAOS bridge报告](../../DAgger/doc/PAOS_BRIDGE_ORIGIN_20260907.md)：来源SSH 10.131.28.55，代码为ziyao-dev下的PhyAgentOS只读接口；具体人员未确认。

用户随后授权停止该bridge；身份复核后SIGTERM正常退出，锁自行释放，未强杀/删锁。

后续工作在独立的 [DAgger](../../DAgger/README.md)。

- [真实启动命令](../../DAgger/START_HERE.md)
- [进度与录制策略详解](../../DAgger/RECORDING_AND_PROGRESS.md)

G2Teleop只保留冻结F710遥操；33–40入口是迁移转发，不再维护第二份实现。
HIL/录制源码、测试和本地离线数据已移至DAgger，原始源码备份及来源manifest也在那里。
历史日志里的原路径不重写；G2原始数据暂留回退副本，新运行全部写DAgger。
本轮仅迁移和无硬件验证，没有新真机验收，没有修复或放宽原有训练审计。
