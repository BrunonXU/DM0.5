# Diagnostics

放置G2健康检查、相机/state预检、O10双手原始状态检查、离线Shadow和历史
当前位置Servo hold诊断。

当前`servo_hold --execute`已被代码永久挡住：G2重启后的O10零反馈不是可靠的
物理保持目标，回发后会按官方零命令张手并让拇指靠近相机。现场首选入口是
`scripts/00_preflight.sh`、`scripts/check_hand_state.sh`；`02_servo_hold.sh`只能预览。
