# G2 Client

正式DM0.5运行时实现。主要数据流是：

```text
cameras + robot_state → observation → dm05_backend
→ safety gates → interpolation → servo
```

`app.py`负责装配，`control_loop.py`负责三种运行状态机。该目录不放临时测试、
复位脚本或训练工具。

Powered启动时，`ServoStreamer`可以先创建线程，但必须等到第一个模型
chunk通过全部安全门后才发送第一组Split目标：双臂14D通过100Hz
`joint_servo_control`，双手20D通过5Hz`move_ee_pos(dual_tool,o10_t2)`。
两部分不重新拼成34D Servo请求；在此之前也绝不用重启后的零手指反馈做hold。
