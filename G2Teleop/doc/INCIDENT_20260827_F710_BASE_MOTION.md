# 2026-08-27 F710 插入后底盘意外运动

状态：开放事故；F710 直接连接 G2 的方案已阻塞。  
现场处置：操作者立即拔除 USB 接收器；禁止重新插入。

## 现象

F710 接收器通过 Type-C 扩展坞连接 G2。同步 `G2Teleop`只读探测文件后，操作者移动手柄，
G2 底盘直接运动。预期只读取手柄事件，不应产生任何机器人动作。

## 证据

1. `G2Teleop` reader 仅以 `os.open(device, os.O_RDONLY)`打开 joystick；代码没有导入
   `agibot_gdk`、没有 GDK Robot、没有底盘或关节写接口。
2. 事故探测日志只有三行启动提示，没有记录任何轴事件，更没有动作输出。
3. G2 从开机起常驻厂商进程：

   ```text
   /home/agi/app/lib/teleop_it/teleop_main_node
   /home/agi/app/bin/remote_hal/pico_adapter
   ```

4. 两个进程分别位于 `vr_control.service`和`remote_hal.service` cgroup；由厂商 launcher
   按 `/home/agi/app/conf/manifest.d/base-remote.json`启动。
5. `teleop_main_node`二进制包含并使用：

   ```text
   joystick
   joystick_control
   chassis_control
   /pnc/remote_control_cmd
   ---Chassis is controlling---
   ```

6. `/home/agi/app/config/user_info.json`存在原厂 joystick 底盘遥控模式配置。
7. 系统日志确认 F710 在 XInput 下由 `xpad`注册；同一 `/dev/input`设备可以被多个进程
   同时读取，本项目的只读打开不会排斥厂商进程。

## 结论

直接原因不是文件同步或 `G2Teleop`下发命令，而是 G2 自带遥操服务自动识别并消费 F710，
把摇杆映射为底盘遥控。最初的安全假设“只读脚本意味着插入设备本身安全”是错误的：在有
常驻消费者的机器人上，USB输入设备的物理接入本身就是会改变控制状态的动作。

当前尚未证明厂商服务实际加载了哪份 G2 YAML。部分 YAML 中 `chassis_control.enable=false`，
另一些为 `true`；现场运动事实优先于静态配置推断。在确认活动配置、服务生命周期和停止服务
影响之前，不修改 YAML、不停止厂商进程、不再做插入实验。

## 恢复条件

F710 方案只有满足以下任一条件后才能恢复：

1. 在与机器人隔离的独立电脑读取 F710，再通过一个具备认证/鉴权/deadman/超时 HOLD 的
   专用通信链发送抽象 teleop intent；或
2. 获得厂商确认的、可验证禁用 joystick/chassis 消费者的方法，并在机器人离地或底盘物理
   隔离条件下完成只读插入验证。

不能使用抢占 `/dev/input`、切换 X/D 模式、调整死区或“先启动 reader 再插 USB”作为安全
解决方案，因为它们存在启动竞态且没有证明能阻止厂商控制链。

