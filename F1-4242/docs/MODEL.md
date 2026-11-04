# F1 实时模型

`web/model-viewer.js` 在姿态页显示原始 F1 URDF / STL 的关节层级。实体读取 `/api/state` 中的 HAL 度数并应用已核验的模型角转换；棕色半透明模型显示编辑目标。拖动模型只改变观察视角，不发送机器人命令。

首次需要下载约 28.3 MB 模型；Three.js 及加载器约 0.74 MB，均由本网站提供，现场浏览器不需要连接外网。模型保留原始几何，没有用示意人形或简化骨架代替。

## 来源与版本

- 自有已保存快照：`F1/pico_upper/assets/f1_wheel/`，源自获准复用的 meaxiao Pico **1.4.4**。
- 原始 URDF：`f1_wheel_0602_new.urdf`，SHA-256 `bc21c6f36446aa2741d6ba20c2190836be967b1f3043dfa39eaae0983c95d5c0`。
- 关节名映射来源：`F1/pico_upper/vendor/f1_safety/src/python/f1_safety_config.yaml`，SHA-256 `83ad2c6bad9686c32c126efdfdfea4b943773c67ed48a4bc9f44e2121c025422`。
- 2026-09-23 经已有 SSH 只读核对，F1-09 的 `Bruno/F1/pico_upper/current` 中上述两份文件哈希与这里一致。同次 `/api/state` 已有 22 个新鲜实测轴。此检查确认工程模型来源一致，不表示厂家控制器正在加载该 URDF，也不替代零位、机构尺寸、夹爪和相机外参的实物验收。
- 31 个 link、30 个 joint；31 份原始 STL 和 1 份 URDF，共 **28,300,673 字节 / 565,196 个三角面**，逐字节未改。来源、原路径、原始哈希与现有哈希见 [`web/models/f1_wheel/manifest.json`](../web/models/f1_wheel/manifest.json)。未访问其他用户当前目录。
- 三维依赖固定为 Three.js **0.170.0 / r170**，MIT 许可证原文及下载来源 / 哈希位于 [`web/vendor/three/`](../web/vendor/three)。仅把 STLLoader、OrbitControls 的 `three` 导入改为本地相对路径。
- 模型快照没有独立的公开分发许可证；当前仅作已获许可的内部复用，不将其声明为 MIT。Three.js 的 MIT 许可证只覆盖其自身代码。

## 度数与坐标

| HAL 名称 | URDF 名称 | 转换 |
|---|---|---|
| `arm_l_j1..7` | `arm_L_joint1..7` | J7 反号，其余保持；再度 × π / 180 |
| `arm_r_j1..7` | `arm_R_joint1..7` | J5/J6 反号，其余保持；再度 × π / 180 |
| `head_j1..3` | `head_joint1..3` | 度 × π / 180 |
| `body_j1..5` | `body_joint5..1` | 名称逆序，度 × π / 180 |

HAL 保留硬件角度定义；模型侧应用 `configs/model_mapping.json` 中经独立原生 FK 核验的三轴符号转换，不改真实反馈、目标或运动命令。不能直接套用 Pico 的手柄/Placo 转换或启动偏置。2026-09-28 核验发现原来的直接 HAL 映射不完整，详细证据及仍存在的几何差异见 [模型审计](MODEL_AUDIT.md)。每个关节严格按 URDF `origin xyz/rpy` 和本地 `axis` 组成父子变换。固定轴 rpy 使用 `Rz(yaw) Ry(pitch) Rx(roll)`。不按历史 URDF 限位截断实测值；控制允许范围仍由后端决定。

需要观察到完整 22 轴才首次显示实测模型。缺失值不填零；后续缺失或过期保留最近有效姿态、降低透明度并显示过期提示。编辑目标仅覆盖明确编辑的轴，其余轴跟随实测；目标与实测使用独立变换树。

轮组没有本轮动态映射，保持 URDF 中的静态外观；模型没有可供当前夹爪百分比驱动的独立手指关节，不捏造开合动画。底盘里程计不应用到此局部关节模型。它是姿态参考，不能用于判断环境碰撞安全、绝对腰部高度或工具端标定精度。

## 前端调用

```js
const {createRobotViewer} = await import('./model-viewer.js');
const viewer = createRobotViewer(document.querySelector('#robot-model'));
viewer.update({
  joints: state.joints, // {arm_l_j1: {position: 90, age_s: 0.01}, ...} 或数字映射
  draft: editedJointDegrees,
  stale: feedbackExpired,
});
// 离开页面时释放 WebGL、事件、ResizeObserver：
viewer.dispose();
```

模型按反馈变化 / 视角变化渲染，不在静止页面永久占用 60 Hz 动画循环。网格资源在同一浏览器标签页内缓存，页面切换释放 WebGL 上下文和材质。

## 准备与验证

在 6000D 新目录中执行：

```bash
python3 scripts/prepare_model.py --check
```

需从自有 Pico 快照重新准备时执行不带 `--check` 的脚本；它不联网、不初始化 ROS，也不会访问历史原路径。发布时一起纳入 `web/models`、`web/vendor` 和模块。

完整浏览器及独立运动学回归（使用已安装 Chrome 与 NumPy）：

```bash
python3 tests/check_model_browser.py
```

2026-09-23 验证：12 项浏览器检查通过；两个末端连杆与头部的 Three.js 世界矩阵，对照独立 NumPy Rodrigues / RPY 正向运动学，最大元素误差 `5.6e-16`。32 份模型文件与原始来源哈希全部一致。证据见 历史 `results/model/validation.json` 和 `results/model/fixture.png`（不随包提供，见 [来源索引](history/SOURCES.md)）。

`tests/model_viewer.html` 是纯浏览器离线夹具；通过 HTTP 提供项目根目录后访问它，可核对 22 轴过滤、真实层级、左右臂隔离、实测 / 目标独立、非有限值保留与反馈过期状态。不触发任何机器人请求。浏览器测试和模型哈希通过不代表真机动作验收。

依赖资料：[Three.js r170 源码](https://github.com/mrdoob/three.js/tree/r170)、[STLLoader](https://github.com/mrdoob/three.js/blob/r170/examples/jsm/loaders/STLLoader.js)、[OrbitControls](https://github.com/mrdoob/three.js/blob/r170/examples/jsm/controls/OrbitControls.js)。


## v12 地面与取景

导航可收起，右侧画布优先占用可用视口，预设单独滚动，并支持放大模型。地面从静态轮底计算一次，不随全身当前包围盒移动；目标躯干/头部包围盒低于地面5mm时标红。后端使用同源几何做采样路径排除，具体范围见 [姿态说明](POSE.md#模型地面检查)。该版未修改原31份STL或零点；2026-09-28 v18 已部署上述双臂模型方向修正，部署状态见 STATUS。前端提示不是执行许可，视觉包围盒也不是完整碰撞网格或物理仿真。

2026-09-28 标准复位模型左末端比右侧高约24.154mm，已定位到原URDF左臂两处18mm偏置与原生运动学不一致；这是名义模型差异，尚未作为实机标定修正。详见 [模型审计](MODEL_AUDIT.md)。
