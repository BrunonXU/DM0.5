# 标定接入合同

**2026-11-05：按用户最新决定，本轮粗标定已完成并交付。** 用户接受当前卷尺与视觉约8mm差异，要求做重复检查后用于首轮开发rollout，后续按真机表现修正。头部／右腕相机数据、当前头身坐标与固定开度的指尖参考已汇入[粗标定交付包](../artifacts/20261105-calibration-continuation/coarse-calibration-handoff.json)，不再以精密认证作为本标定线程的交付阻塞。

本页是标定与Harness集成之间的接线合同。粗标定数据可供初轮调试接入；实际服务装配、反馈检查和现场START仍归控制所有者。本次交付没有改变正式精密bundle、生产参数或运行保护，也没有把重复性结果称为绝对物理精度。

## 当前粗标定交付

- 数据身份：`coarse-aea05cd220e75f10`；[主JSON](../artifacts/20261105-calibration-continuation/coarse-calibration-handoff.json)包含相机K/D、RGB-D变换、实际IK坐标下的手眼、当前头身参考、指尖变换、设备身份、来源哈希和检查结果。
- 使用实际URDF／Pinocchio `base_link/world → arm_R_Link7`；右腕链为`T_world_Link7 × T_Link7_right_camera`，工具链为`T_world_Link7 × T_Link7_tip_reference`。不要混用原生SDK `native_right` 的311／314mm差异链。
- 指尖参考是在HAL闭合反馈**18**时，两内侧前缘对应截面的中点，坐标轴保持Link7定义。`T_Link7_tip_reference`平移约`[2.792,-236.286,8.557]mm`；改变开度或夹住物体后，按视觉反馈更新实际参考／持物偏移。
- 复核包含原25姿态、5组留出，以及本次5段静止RGB-D、13次空夹爪小步和两次返回同开度。两回程中心重现差约0.188／0.384mm；这表示本次重复性，不能当作绝对误差上界。
- 用户卷尺净距110mm，图像参考净距约101.78mm，差8.22mm原样保留，没有缩放相机或改矩阵去凑数。头部新身体姿态使用名义URDF传播，适用姿态写在包内。
- 当前状态为`coarse/development`、`accepted_by_user=true`。该文件是开发用几何数据包，不是下节的正式九报告runtime bundle；精密插孔、在手物体偏移与任务结果分别在后续rollout中检查。详见[现场复盘](../agent/experiments/20261104-camera-handeye.md#按用户决定交付粗标定两次回程验证完成)。

## 数据与坐标

- 所有刚体变换使用 `T_parent_child`，行优先 4×4，平移单位米，旋转为正交右手矩阵。
- 头部 RGB 1280×720；Depth 848×480。右腕 RGB／Depth 均为 640×480。Harness ROI 固定 `[307,28,1229,720)`，几何使用原分辨率。
- 需要头部／右腕内参、畸变、深度单位、Depth→RGB 外参、头部→原生基坐标、右腕手眼、原生 EEF→TCP、在手物体偏移、孔口及桌面／桶几何。
- 保存相机设备身份、分辨率、安装与工具身份、采集 boot ID、源时间域、身体／腰／头参考姿态。挂载、工具或相关参考变化须使旧标定失效。
- 尺度来自实际量尺。棋盘10×7方格、9×6内角点、名义22mm；用户卷尺初核为横向8格176mm、纵向4格87.5–88mm。支持原尺寸打印，尚非毫米级尺度精度认证，详见[标定实测与敏感度](../agent/experiments/20261104-camera-handeye.md#继续验收打印实测坐标链与深度复核)。

## Bundle 输入

`f1-harness calibrate bundle bundle.json` 调用 `calibration.validate_bundle`。Bundle 需要九份报告：

| 字段 | 证明内容 |
|---|---|
| `head_intrinsics`、`right_intrinsics` | 当前分辨率的内参／畸变与独立留出检查 |
| `right_handeye` | 右腕相机到原生 EEF 的手眼及多轴留出验证 |
| `tcp` | 原生 EEF 到真实 TCP 的测量与验证 |
| `head_extrinsics` | 头部相机到机器人原生参考系 |
| `rgbd_registration` | 深度尺度、RGB-D 外参与对齐误差 |
| `native_frame_chain` | 身体／腰／头及右臂坐标链的物理一致性 |
| `scene_geometry` | 桶／桌面／孔口／夹持物与邻物几何、允许插入区域 |
| `tracking_validation` | 实际跟踪误差、停稳判据及插孔误差预算 |

每份报告需 `accepted: true`、非 synthetic、真实证据引用或独立 holdout 身份。总文件提供 `effective_clearance_m` 和 `combined_error_bound_m`，要求 `0 < error < clearance`。这只是文件准入检查；审阅报告真实性、设备绑定与实测覆盖仍是部署验收的一部分。

Bundle 内容摘要成为 `calibration_id`，同时绑定 Task、Observation、SkillIntent 和数值结果，不能分别填不同 ID。`owner.example.json` 默认 `calibration_bundle: null`、`integration_validated: false`，不能靠修改这两个字段跳过真实集成检查。

## 可复用工具与现场证据

`calibrate intrinsics / handeye / tcp / rigid` 分别接收对应函数的 JSON 参数，求解与 holdout 分离；`calibrate bundle` 检查整体准入。Python 函数签名见 `src/f1_harness/calibration.py`。`scripts/ros_capture.py` 在 F1 原生 ROS Python 下只读采集，明确零命令 publisher；不要把 6000D Python 3.11 环境搬到 F1 原生 Python 3.12／Jazzy。

本轮只读样本在 `HARNESS/artifacts/native-capture-20261104/`，包含原始字节、CameraInfo、关节反馈与时钟信息。三路 RGB-D 均收到，头部最后一对接收时间差约 66 ms，腕部约 1.8 ms；这些接收时间并不证明曝光同步。当前统一 `geometry_admitted=false`，深度单位亦未由本轮实测认证。

2026-11-04实测接续已提供[相机接入审阅包](../artifacts/20261104-handeye-session/continuation/camera-integration-candidate.json)：直接用实际Pinocchio的`base_link/world → arm_R_Link7`生成相机候选，并绑定URDF／映射哈希及头身参考。它与原生`native_right → EEF`候选分开，不能混用；不借固定变换掩盖311／314mm几何差异。新增静止画面的头／腕一致性最大约0.31mm，但右腕深度典型帧间波动约2.55mm、真实TCP未验收，因此仍为非运行候选。见[实测接续](../agent/experiments/20261104-camera-handeye.md#进一步接续执行器坐标候选与新位置静止复核)。

## 插入几何额外交付

几何技能还需要 `object_grasp_eef`、孔口法向、插入深度、接近／转移净空、附着物包围半径、夹爪开度、原生 frame 名、关节到位容差和停稳窗口。参数不得由 planner 自填。桶应分解为保守障碍与已验证的孔口通道；把整个桶当实心 AABB 会拒绝插入，把整个桶移出障碍则丢失碰撞约束。

在手姿态需独立视觉证据，不能用夹爪中心替代物体中心。精确插入前还需可靠的 `held`／`inserted`／匹配 `hole_id` 与释放后稳定判据；缺失时返回待审。

## 实际IK接口的两端转换

本轮代码已经强制要求有独立证据的 `frame_chain`，但没有真实转换矩阵的准入结论。最新手眼候选的原生SDK参考系和默认EEF，不能直接当作URDF/Pink的world与 `arm_R_Link7`。

`plan.fixture.frame_chain` 与 `gateway.scene.frame_chain` 必须相同，提供 `T_ik_world_native`、`T_ik_eef_native_eef`，同时登记 `native_frame`、`ik_world_frame`、`native_eef_frame`、`ik_eef_frame`、独立验证 `holdout_hash` 与 `evidence_ids`。实物核对通过后才能标记accepted，synthetic必须为false。

公式是 `T_native_eef = inverse(T_ik_world_native) × T_ik_world_ik_eef × T_ik_eef_native_eef`。几何目标转换和在手偏移分别使用对应的左右乘关系；原生参考系中的障碍包围体也一起变换。单纯改frame字符串或默认为单位阵都不允许。

控制端另需实测 `stop_delta_deg`、`stop_window_ms`、`stop_gripper_delta` 和技能 `gripper_tolerance`。闭爪到位、取消后夹爪停稳与手臂停稳分别检查，不用夹爪命令值代替实际反馈。
