# Shell公共库

`common.sh`负责GDK环境、75K/7892端口检查、进程互斥和统一日志目录。
`powered_split_runner.sh`负责同步/异步Split入口的时间参数、请求数、确认倒计时和
客户端参数。二者只供现场脚本`source`，不要单独执行。
