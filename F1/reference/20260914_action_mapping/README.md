# F1 动作映射：第一批选定源码证据

2026-09-14，经已验证的 SSH 隧道只读从 F1 的 ms-thor 获取 4 个明确文件，
原样内容共 **149695 字节**。4/4 文件的远端内容 SHA256、接收大小和落盘后 SHA256 一致；
逐文件原路径、解析路径、UTC 抓取时间、大小、SHA256 与 Git 信息见 [manifest.json](manifest.json)。
4 个源文件本身均不是软链接，解析路径与源路径相同。

本目录是选定文件的参考证据，**不是整个项目备份或可运行环境**。
没有修改 F1、启动 ROS、导入控制模块或发送机器人动作；没有读取进程环境变量或凭据文件。
传输前执行了疑似硬编码凭据/私钥的启发式检查，4 个文件未被该检查标记；
该检查不能替代完整的安全审查。

## 文件与版本

| 文件 | 字节 | Git 证据 |
|---|---:|---|
| [base_teleop_controller.py](files/home/user/meaxiao/pico/vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller.py) | 66391 | 未取得 Git 元数据 |
| [send2real_robot_f1_udp2topic.py](files/home/user/meaxiao/pico/vendor/f1_bridge/send2real_robot_f1_udp2topic.py) | 11984 | 未取得 Git 元数据 |
| [motor_ctl.launch.xml](files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/launch/motor_ctl.launch.xml) | 6345 | 未取得 Git 元数据 |
| [arm_replay_node.cpp](files/home/user/zxl/src/arm_replay/src/arm_replay_node.cpp) | 64975 | 所属 /home/user/zxl 的 HEAD 为 3f903739bc26fb49d0a5addf41c5cd91929b4f6b；仓库有受跟踪修改，该选定文件未显示修改 |

Git dirty 检查覆盖受跟踪文件；未扫描其他文件的 untracked 状态。
保存的是抓取时实际内容，不以 commit 代替未提交工作区。
这些文件的版本尚未证明就是泡沫棉数据采集时使用的版本。

## 初读线索

1. base_teleop_controller.py 第 328–332 行定义 /lead 的名字为
   arm_L_joint1..7 后接 arm_R_joint1..7，第 352–354 行创建 /lead/joint_states 发布者。
   但发布循环定义不在此文件中：第 20 行导入 Ros2TeleopMixin，第 543 行启动 self._publish_loop。
   因而仅靠这一份文件不能确定 /lead 数值是在转换、保护或滤波之前还是之后发布。

2. 该控制器第 19 行使用同目录 send2real_robot_f1_udp 模块作为 UDP 发送端。
   sync_placo_to_real 的第 960–982 行显示反馈到 Placo 模型存在度转弧度、左 J1 减 180、
   左 J2/J4 变号、右 J6/J7 变号；这是该同步函数中的转换，
   **不能直接反推 /lead 到执行端的转换**。第 1387–1390 行的 _send_command 是抽象方法，
   具体实现亦未在这 4 个文件中闭合。

3. UDP 桥第 44–48 行使用执行器名字 arm_l_j1..7 后接 arm_r_j1..7。
   第 195 行按 float64 读取 UDP；第 271–293 行对 14/16 维帧取前 7、后 7 作为双臂，
   第 114–116 行直接将 left_q + right_q 写入 /motion_ctl/joint_ctl 的 position。
   **这段桥接路径中没有单位/符号/偏置转换**，所需转换若存在，应继续核对上游发送端和发布 mixin。
   limit_joint_left/right 在第 215/244 行有定义，但本文件处理路径没有调用它们；
   不能据此认定 J6 已被有效裁剪。send_both_arms_and_grippers 虽接收夹爪参数，
   当前第 98–120 行函数体只发布关节，夹爪路径仍需独立核对。

4. 已安装 motor_ctl.launch.xml 中双臂 J6 写为 [-60, 60] 度，
   J2 写为 [-94, 120] 度，与接口文档中部分版本的 J2 范围不同；
   joint_state_publish_period_ms 为 20。这些是文件值，不是已读出的运行参数，
   也不是过去采集频率或硬件真实限位的证明。

5. arm_replay_node.cpp 第 104–106 行将 /hal/joint_states 和双夹爪反馈作为回放来源；
   第 1251–1326 行按关节名提取反馈，加入头部目标及插值后发布。
   这是已有的反馈轨迹回放，不能直接等同于本任务从 /lead、夹爪命令和后补底盘
   组成的 19D 训练动作回放；本次未验证其配置、调用条件和底盘支持。

## 下一步定点核对

优先追踪 Ros2TeleopMixin 的 _publish_loop 与同目录 UDP 发送端，确认 /lead 发布位置、
物理单位及发送到执行器的数值是否相同；随后对照现有数据中 5 条 J6 超出文档范围的轨迹。
本轮没有扩展抓取这些依赖，也没有修改任何数据标签或清洗阈值。
是否为采集版本、控制器实际参数、完整保护链、身体机构映射和相机时钟仍待核对。
