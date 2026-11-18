# 非标定baseline软件收尾与原生安装

**2026-11-05：阶段感知、服务装配和结果封存软件已实现，127项Harness回归通过。** 新独立开发包已安装F1，原生导入与纯模型配置准备通过；固定程序的模拟抓空＋插入恢复再次完成。本轮没有调用Astra、训练、替换生产入口或发布机器人动作。

**还不能开始自主插孔。** 8秒真实全帧只读探针复现头部重复源时间戳；标定、现场检测参数和完整真实反馈shadow尚待接入。合成感知、合成视频和模型preset数值准备均不代表物理闭环成功。下一步按[TODO](../TODO.md)完成现场门槛，操作见[装配说明](../../docs/SITE_STARTUP.md)。

## 1. 最终实现

| 部分 | 本轮变化 | 实测边界 |
|---|---|---|
| 感知 | owner提供任务generation／epoch／阶段；持物与停稳分开；正常孔口遮挡由当前独立固定桶特征及期限约束；fixed_color轻量检测可选 | 颜色、搜索区域、桶特征和容差未实际登记，物理抓住／插入尚未验证 |
| 生命周期 | 完成grasp后请求ID变None不清持物；同一授权内阶段切换丢竞态帧但保留已测历史；撤权和身份变化清旧证据 | 真实控制接管／停稳仍待现场 |
| 服务 | 配置草稿、辅助进程监督、失败清理、原生源码固定、独立owner入口；prepare-native自动生成可信路径和纯数值spec | 进程准备不授予运动，也不证明标定几何有效 |
| 封存 | Harness复用原journal／payload，独立图像连接有界归档；seal核对发布、反馈、身份和图像哈希；视频复用已有renderer | 视频测试使用合成文件，无实际任务视频 |
| 统计 | 未封存真实run不报自主成功；正式20场景评估要求完整封存及冻结场景绑定；任务结束后的人工复位不污染该任务 | 尚无真实自主成功样本 |
| 时间诊断 | 新增全帧metadata探针和inspect-timing；保留源／接收时间差异，不改写时间戳 | 统计不认证曝光同步或绝对延迟 |

遮挡参照合同可使用桶上现成特征或标记：至少三个非共线、可独立识别的特征，当前RGB-D位置／深度匹配，验证绑定当前标定ID并设置有限遮挡期限。默认关闭，不把不可见孔口伪装为直接可见，也不要求重新采集整套手眼。

## 2. 测试与独立审查

- [最终Harness回归](../../artifacts/noncalibration-20261105/harness-final.xml)：**127 passed**；包含场景序列、真实本机HTTP／Unix socket、隔离worker、服务起停、打包完整性和合成封存。
- [DAgger实际CLI／录制回归](../../artifacts/rollout-evidence-20261105/dagger-entry-final.xml)：**39 passed，4 skipped**。跳过项为既有环境依赖，未计通过；shadow仍不产生视频／逐帧训练录制。
- [固定程序完整模拟恢复](../../artifacts/noncalibration-20261105/baseline-recovery/result.json)：抓空＋插入失败注入后完成，`synthetic=true`，不调用模型。
- [独立感知环境入口](../../artifacts/noncalibration-20261105/fixed-color-entry-probe/receipt.json)：使用perception环境真正启动mask服务，合成图片返回正确掩码，SIGINT退出；没有借用ARM依赖或核心venv。
- [固定颜色CPU探针](../../artifacts/noncalibration-20261105/fixed-color-benchmark.json)：922×692、100帧合成单物块，中位1.43ms／P95 2.12ms。只计检测器，不含RGB-D注册、网络、归档或数值服务，不代表500ms整链准入。
- [浏览器验收](../../artifacts/noncalibration-20261105/browser/browser-report.json)：4243两篇操作说明、表格、Mermaid、目录和后退通过；无外部请求或写请求。

独立审查确实找到了三类测试遗漏，并已修复后重新回归：grasp完成时清持物、正常阶段竞态误清历史、旧CLI强制inference-only与新完整录制要求冲突。另将封存任务范围在完成／会话关闭时截断，防止后置人工复位污染自主统计。首轮116项回执保留，最终以127项为准。

本机socket测试初次在沙箱内因权限被拒绝，随后仅对本机测试使用允许socket／bwrap的执行环境通过；没有放宽机器人保护。原生准备成功后的回执取回曾使用错目录，随后从实际run补取，没有重跑模型准备或动作。

## 3. 原生包与站点

本次是未分配产品V号的开发候选，不能作为已验证生产发布。

- [包清单](../../artifacts/noncalibration-20261105/native-bundle/manifest.json)：`968bbce9ebefe1be17da1389e5f143d4dbe35dda4e9ec674cccfd2a6e24d435c`，254个文件；固定Harness、RPent、F1 owner和共享组件。
- 安装目录：`/home/user/Bruno/HARNESS/development/968bbce9ebef`。原始生产V47入口未切换，既有venv未混装，依赖位于新包的deps。
- [原生导入结果](../../artifacts/noncalibration-20261105/native-check.json)：Python3.12.3／aarch64，ROS、OpenCV、RPent通路及owner／numerics模块通过；F1控制源码确实来自新包的control/src。
- [纯模型准备回执](../../artifacts/noncalibration-20261105/prepared-native-site/native-preparation.json)：沿用profile preset[1]构造原CollisionGate并导出数值spec，非实时姿态。数值阈值0.5mm／0.005rad是求解器设置，不是机器人精度。
- 机器人站点：`/home/user/Bruno/HARNESS/site/square-968bbce9ebef`；本地草稿：`site/f1-square`。`controller.local.json`没有accepted release身份且`ik.verified=false`；没有修改标定准入字段。
- [准备后readiness](../../artifacts/noncalibration-20261105/site-readiness-prepared/result.json)：numerics和各进程socket绑定通过；其余未测字段明确拒绝，不把监听或安装成功视为动作授权。

## 4. 真机只读时间证据

使用新脚本 `scripts/camera_timing_probe.py` 仅订阅相机，8秒保留775条全帧元数据，不存像素、不发布命令。原件见[全帧采集](../../artifacts/noncalibration-20261105/live-timing/capture.json)，统计见[时间报告](../../artifacts/noncalibration-20261105/live-timing-report/result.json)。

| 流 | 收到帧数 | 重复源时间戳间隔 |
|---|---:|---:|
| 头RGB | 166 | 1 |
| 头Depth | 204 | 5 |
| 右腕RGB | 210 | 0 |
| 右腕Depth | 195 | 0 |

无反向时间戳。这个窗口没有证明右腕全局可靠；掉帧、源时钟与曝光同步仍分别验证。此前约0.5秒间隔保留的6帧没有重复，不能推导实时流没有重复。没有用接收时间代替源时间、设source_clock_verified=true或延长原准入时限。

## 5. 接续

接入标定交付与孔口间隙；登记检测区域、桶参照和实际容差；处理头部源时间质量并测整条感知时效；运行真实全链shadow，再现场START逐阶段验证。第一条结束后取回owner原件并seal，保留所有失败；先5条开发结果，再开展固定程序／Astra同场景对照和20场景正式验收。

当前没有真实Harness rollout、真实经验晋升或物理成功率结论；本轮软件成果和标定线程的现场动作分别记录，不合并冒充自主任务。
