# 启动锁检查修复

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

用户报告40入口倒计时、加载GDK后报PID2481159占用。只读ps确认该PID仍存活，运行`g2_paos bridge --g2-read-only --operator-confirmed-exclusive`；并非可删除的残留PID文件。本轮没有终止该进程或删除锁。

用户再次质疑是否40遗留后复核：PID2481159启动于17:21:35，父进程2175265为16:19:35启动的交互bash，终端pts/2，工作目录/home/agi/xza/DM0.5；进程扫描没有存活的40入口（排除本次查询命令自身）。40及其Powered HIL模块没有启动g2_paos/subprocess路径。证据支持这是另一终端的bridge，而非40的遗留子进程；当前终端取消40命令不会向pts/2的bridge发送Ctrl-C。

修复40入口：预检首先只读检查锁及存活进程，直接打印PID/命令并返回2；正式启动仍使用共享客户端的原子acquire，移到GDK导入之前，覆盖用户确认期间出现的新owner。获取锁后导入或telemetry初始化失败会释放自己的锁。已有运行清理逻辑保留，不在采集线程仍可能调用GDK时强行释放锁。

验证：本地3项、G2 3项测试通过（活进程锁保留、失效进程交给原子acquire、半写锁不删除）；Python编译、shell语法通过。G2实际40 files-only在同一活进程占用下立即HOLD/退出2，无GDK初始化、无倒计时。用户需先在bridge原终端退出它，才能取得共享Robot使用权。
