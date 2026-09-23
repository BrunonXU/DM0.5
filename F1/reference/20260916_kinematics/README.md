---
created: 2026-09-16
type: reference_snapshot
status: captured_read_only
---

# F1原生运动学参考快照

通过已授权SSH隧道只读取得，文件原路径、采集时间与SHA见[manifest.json](manifest.json)。没有修改源工作区或运行参数，没有发送控制命令。

- [binary_sha256.txt](binary_sha256.txt)绑定运行中的`/proc/36426/exe`与安装`kinematics_node`：二者SHA均为`c53411708ea0a7f94c3df54d5e333de4ccd79f610ad81a099bca4adb2af79fc6`。
- 当前`libKine.so`为`7f03a33964123d2b37d249a51cbdb748c54f5445021ac10e128b27f349dc4d83`；仅记录指纹，不把ARM二进制当作6000D可运行库。
- `/kinematics`参数只读查询确认加载`/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg`，启动参数文件也指向该路径。
- 已安装的`ForwardKinematics.srv`约定关节度、位姿mm；`InverseKinematics.srv`规定`target_xyzabc`长度为6时优先，使用矩阵应将其留空。
- [选择范围反汇编](kinematics_callbacks.disassembly.txt)显示初始化调用`LOADMvCfg`、`FX_Robot_Init_Type/Kine/Lmt`；IK参数清零992字节，先调用普通IK，只有NSP启用且角度非零才另调用IK_NSP。
- `LOADMvCfg`输出的PNVA列序是正限、负限、速度、加速度，与文本配置中负限先出现的排列不同。独立进程诊断须用加载后的语义。
- 当前SDK矩形限位与motor_ctl不相同，且有J6/J7耦合边界；服务`success`与`is_joint_exceeded`必须分别判断。

隔离SDK与现有服务10组数值对照通过，完整数据/报告见[运动学验证](../../foam_sort_whole_body/results/kinematics_validation_20260916/README.md)。此快照证明本次版本与数值行为，不证明采集时同版本或实机几何标定精度。
