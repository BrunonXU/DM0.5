# G2 Client

正式DM0.5运行时实现。主要数据流是：

```text
cameras + robot_state → observation → dm05_backend
→ safety gates → interpolation → servo
```

`app.py`负责装配，`control_loop.py`负责三种运行状态机；正式Sync的双手不再使用
`shared_preview_v1`，而是由5Hz手线程直接采样当前模型原始20D目标。目标与上次成功提交值
完全相同时只记录`hand_command_skipped`，不再次调用点目标接口。RTC仍保留自己的独立配置。
该目录不放临时测试、复位脚本或训练工具。

Powered启动时，`ServoStreamer`可以先创建线程，但必须等到第一个模型
chunk通过全部安全门后才发送第一组Split目标：双臂14D通过100Hz
`joint_servo_control`；正式Sync当前双手20D以5Hz采样原始连续目标，仅在变化时通过
`move_ee_pos(dual_tool,o10_t2)`发送，其他模式保持各自配置。
两部分不重新拼成34D Servo请求；在此之前也绝不用重启后的零手指反馈做hold。

正式Sync的双臂链路为weight10 → C2 → 120ms边界融合 →
`stationary_minjerk_v1/0.0035rad`。最后一步只投影最终100Hz目标中反复换向且活动范围很小的
单轴，使用同起终点minimum-jerk曲线；底层CLI默认`off`，只有正式05入口显式启用。
`arm_micro_motion_projection`记录每个chunk的触发轴和最大修改量，RTC入口当前仍关闭。

## 三层运动诊断

`DispatchTelemetry`对Powered Sync和RTC都保存双臂逐条成功命令、segment单调起点、实际发送
offset和GDK调用耗时。`joint_feedback.py`只在
`logs/diagnostics/joint_feedback_probe_latest.json`表明10或20Hz通过时启用反馈sidecar；可用
`--disable-joint-feedback`强制command-only。

反馈读取不创建第二个Robot或GDK线程，而是在已成功的arm发送后由唯一Controller按probe频率
执行；writer线程只消费内存队列。单次读取超过5ms、读取失败或连续两个arm间隔超过15ms时
锁存关闭采样但不停止控制。该机制尚需G2只读probe和短Powered时序门，不能仅凭代码存在宣称
不会影响100Hz。
