# Harness 软件搭建与闭环验收

**2026-11-04：真实 Astra medium 驱动的隔离模拟闭环已通过，含插入失败恢复和经验快照读取；当前源码的 Harness 45 项、DAgger 31 项回归通过。** 原生 F1 环境的纯 FK／IK 与模拟环境障碍拒绝也已运行。这些证据支持继续现场集成，不代表真实抓取插孔成功。

本报告只覆盖软件实现分支：没有触发真机动作、训练或生产切换。另一 agent 正在进行用户另外授权的标定动作与采集，参见[标定记录](20261104-camera-handeye.md)；不能将本报告的“零动作”扩大为整个项目当天没有动作。

下一步是原生服务环境装配、实测标定准入、完整真实反馈 shadow 与现场单块验收。当前实时场景服务虽已接线实现，但尚无全链真实运行证据；CPU SAM2 smoke 也不是实时性能验收。

## 实际实现

- 独立 Python 3.11.18、uv 0.13.7、核心与感知两份 `uv.lock`；四个上游锁定提交，原件保持 clean。
- PyRUA `ArmToolkit`／MCP／提示与统计、RPent Toolkit／HTTP RPC／MemoryManager；薄 Responses planner 只暴露 `python` 与 `finish`。F1 registry 使用生成 overlay。
- bwrap 隔离 worker；凭据只在 planner；宿主 HOME、API 文件、设备和宿主进程环境不可见；无限循环由外部终止。worker 设内存与单文件上限，程序和各技能分别有界。
- owner 会话／任务／epoch／幂等／技能前置条件／恢复预算；失败插入必须撤离和重新对准，不能换 request ID 绕过；取消分别记录请求、owner 保持与实测停稳。累计漂移会重置停稳窗口。
- ROS 只读 RGB-D 合同、独立图像通道、有界内存帧缓存；标定场景生产、SAM2 独立进程图像接口、原生 FK 查询、独立后置条件验证。图像源时间不随处理完成而刷新；超时场景拒绝运动准入。
- 几何技能包含闭爪后的有界抬升，供视觉检查物体是否随夹爪移动；末端目标包含在手偏移；实际 IK 中间路径增加环境包围体检查，原机器人碰撞保护保留。
- DAgger 开发树接入 `harness`／`harness-shadow` 和 `harness_skill`；原 owner 唯一发布。新增 `run-task` 只准备任务并等待实际 owner START，默认离线配置不能调用该入口。
- 候选经验、独立运行验证、拒绝／晋升、固定快照与 20 场景评估器；真实和 synthetic 分区，经验不生成运动代码或训练权重。

## 验收证据

| 检查 | 实际结果 | 原件 |
|---|---|---|
| Harness 回归 | 45 passed，含隔离执行、完整 HTTP→UDS→owner 模拟闭环、故障分支、场景合同 | [JUnit](../../artifacts/software-acceptance-20261104/harness.xml) |
| DAgger 回归 | 31 passed；Harness、runtime、shadow、output | [JUnit](../../artifacts/software-acceptance-20261104/dagger.xml) |
| API 协议 | 官方 `gpt-6-astra / medium`，红色图片识别及 function result 往返，零动作 | [结果](../../runs/20261104T022607-9c19a726/result.json) |
| Astra 模拟恢复 | 插入失败后恢复，13 个技能、3 次 Python cell 加 finish | [结果](../../runs/20261104T023635-60ddfd1e/result.json) |
| 后续快照读取 | 独立 synthetic 候选验证与 RPent 晋升后，下一次 Astra 读取固定快照并完成恢复 | [经验回执](../../artifacts/synthetic-experience-20261104/receipt.json)、[运行结果](../../runs/20261104T031426-c52a7f17/result.json) |
| 原生相机 | 三路 RGB、Depth、CameraInfo、关节反馈只读样本 | [检查](../../artifacts/native-capture-20261104/inspection.json) |
| SAM2 CPU | ROI 提示分割和三帧跟踪成功；无 metric pose 准入 | [结果](../../artifacts/sam2-cpu-smoke-20261104/result.json) |
| F1 原生数值 | Python 3.12.3，保持／抬高 2 mm 求解通过；模拟障碍物被实际 IK 路径检查拒绝；零 publisher／零动作 | [最终结果](../../artifacts/native-numerics-20261104/result-final.json)、[日志](../../artifacts/native-numerics-20261104/console-final.txt) |

两次真实模型驱动的 synthetic 恢复各用了 4 次 API 调用，模型响应累计分别约 **12.86 s、16.58 s**，总 token 分别为 **16,342、16,922**。这些是单次运行记录，不能推出经验使效率提高，也没有进行 reasoning effort 对照。[指标原件](../../artifacts/software-acceptance-20261104/model-metrics.json)。

SAM2 tiny 在 CPU 上首个分割约 3.51 s，三帧视频流程约 6.64 s，包含相应加载／流程开销，不能当作 GPU 稳态延迟。该速度尚不能满足 500 ms 运动观测准入；未为了通过而放宽门槛。CUDA 扩展没有构建，官方实现跳过相应 mask 后处理。

原生数值最终检查中，抬高 2 mm 求解约 **14.85 ms**，末端模型误差 **0.400 mm**。输入为 profile 保存姿态，不是实时反馈；这是求解误差，**不是机械精度、手眼误差或插孔精度**。

## 问题、修正与证据边界

1. 外层 sandbox 禁止本机 socket／user namespace 时测试失败；在允许这些本机测试能力的执行环境重跑通过，没有添加宿主执行降级。
2. RPent `ToolResult` 起初不能直接跨 JSON 通道，改为传递 `.result`；模型遇到缺少几何的模拟场景曾返回待审，补齐明确标记的合成几何后重测。失败原 run 保留。
3. 初版每技能请求模型导致轮次耗尽；改为组合有结果检查的 Python 程序，保留有界恢复。真实硬件程序预算与模拟程序预算分开，各技能仍有单独截止时间。
4. 发现服务配置参数重名、重复动作绕过恢复与逐帧漂移误判停稳，已修正并加入回归。
5. 原生探针最初按开发目录 `F1/src` 导入失败；核对冻结包实际为 `src` 后通过。测试只写独立 `/tmp/f1-harness-native-numerics-20261104`，未改发布包。
6. 原生 venv 已有 Pinocchio／Pink，但没有完整 Harness 依赖；此次纯数值探针不等于所有 ROS、网关、验证服务已经在原生环境装好。ROS Jazzy 的 Python 3.12 与核心 3.11 环境不可混拷。
7. 实时 SAM2 接口当前使用提示框传播，另有保存窗口视频跟踪器；没有完成杂乱堆叠下的稳定身份、遮挡恢复或泛化验证。无法确认物体／孔口／在手姿态时拒绝推进。
8. 未完成真实反馈全链 shadow、实测 20 场景冻结、18/20 真机验收或真实经验晋升。没有任何完整任务能力改善结论。

## 可接续产物

人类命令见[运行说明](../../docs/RUNBOOK.md)，标定格式见[交接合同](../../docs/CALIBRATION_HANDOFF.md)，配置模板位于 `configs/*.example.json`。模板中的 `null` 和替换标记明确表示缺少实测输入，不是默认有效参数。

开发清单为 `artifacts/software-acceptance-20261104/development-manifest.json`，记录源码与锁文件身份；它不是生产 release，产品 V 号尚未分配。源代码处于共享未提交工作树，保留其他人的改动，不自动提交或推送。

## 首个真机 baseline 的追加审阅

用户要求将下一轮重心放到可直接开始真机rollout并得到结果。此次并行只读审阅确认：之前的软件与纯数值证据有效，但仍不足以表示真实闭环已经接通。没有在此次审阅中修改控制代码或新增测试通过结论。

关键缺口分别位于 `owner_bridge.py`／DAgger `harness_runtime.py` 的反馈及夹爪阶段、`harness_compute.py` 与标定 `analyze_capture.py` 的不同坐标参考、`mask_service.py`／`scene_pipeline.py` 的遮挡和历史窗口、`task_runner.py` 的shadow入口，以及 `evaluation.py` 的人工辅助识别。要同时核对程序的实际状态流转，不能只检查接口名称是否存在。

实施优先级已合入[当时方案的首条真机运行优先级](20261112-implementation-plan.md#01-首条真机运行前的优先级)与TODO。目标是固定范围内首批5条开发rollout，不把未来独立20场景验收提前作为首条运行的门槛；所有实测准入与原owner保护仍保持。

## 首次运行入口与原生装配补证

本轮执行了此前审阅中的一部分修复，未将其余缺口写成完成：非POLICY反馈刷新、夹爪实测到位／时效、累计停稳、任务级人工辅助标记已实现；显式两端坐标转换、原生障碍转换、shadow数值候选probe、配置检查、结果摘要与owner事件导出已补齐。原生全链shadow、遮挡／在手证据、真实视频与物理几何准入仍未通过。

- [Harness最终回执](../../artifacts/baseline-startup-20261104/harness-complete.xml)：55 passed。早一轮shadow测试发现事件参数 `kind` 重名，修复后通过，失败原回执保留。
- [DAgger回执](../../artifacts/baseline-startup-20261104/dagger.xml)：32 passed，包含非POLICY反馈刷新；生产未切换。
- [固定技能模拟结果](../../artifacts/baseline-startup-20261104/demo-baseline/result.json)：抓空＋插入失败恢复后成功。
- [真实Astra模拟结果](../../artifacts/baseline-startup-20261104/demo-astra/result.json)：4次真实模型调用，插入失败恢复成功；synthetic始终为true。
- [原生依赖导入](../../artifacts/native-baseline-20261104/import-check.json)：aarch64／Python3.12.3，所有指定模块可导入，原NumPy2.2.6／SciPy1.18.1保留；追加依赖只在独立deps目录。
- [真实只读RPC](../../artifacts/native-baseline-20261104/readonly-probe.json)：8秒79次查询，头／右腕4路RGB-D收到，零publisher。头部出现duplicate_source_stamp和source_time_not_advancing，geometry_admitted=false；右腕仅receipt_only。
- [原生开发包身份](../../artifacts/native-baseline-20261104/bundle/manifest.json)：`6bc1681a6249`，位于F1 `/home/user/Bruno/HARNESS/development/6bc1681a6249`。不包含API凭据、模型权重或训练原件；不是生产release，不包含它构建后才增加的本地接口。
- [配置缺项回执](../../artifacts/baseline-startup-20261104/site-readiness/result.json)：缺少6组local配置，明确未就绪，退出码2，不触发动作。

人类首次操作说明见[FIRST_RUN](../../docs/FIRST_RUN.md)，已登记Why&How阅读目录。单run报告和owner事件导出不等于完成统一真实视频封存；正式启动包与现场Enter／START仍待后续准入，不提供能绕过这些门槛的命令。
