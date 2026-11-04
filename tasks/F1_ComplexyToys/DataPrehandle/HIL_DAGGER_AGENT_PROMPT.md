# RTC / continuous 接入 HIL-DAgger：Agent实施交接

**目标是把现有RTC策略接入真实人工接管，得到可审核、可训练的纠正数据。** 当前独立continuous入口不能手柄接管，已有同步HIL与录制组件可以复用；这份交接不是“已经上线”的声明。

下面正文可整体交给实施agent。完成代码、隔离部署及离线验收后，由用户配合真实手柄shadow并现场执行短接管；首条数据审核通过再批量采集。本任务不启动新训练，不改独立IK实验，也不实现合成末帧增强。

更新：2026-10-21；历史路径和参数需先与当前STATUS、实际代码及服务身份核对。

## 可直接交给Agent的任务

请在`/home/user/projects`完成F1积木的RTC + HIL-DAgger接入：策略连续运行，真实F710人工接管纠正，明确交还后恢复RTC，整段录制并导出可信专家标签。请实际实现、测试、准备独立部署及命令，不停留在方案。已有授权内直接推进；需要现场手柄或真实动作时，先完成其余工作并列出待验步骤，不能伪造现场通过。

### 1. 先读当前上下文

按顺序读取，不把历史计划当现状：

1. Projects的`AGENTS.md`、`PROJECT_RULES_AND_PREFERENCES.md`。
2. `tasks/F1_ComplexyToys/AGENTS.md`、`STATUS.md`、`V1_Summry.md`。
3. 同任务`INFERENCE_RECIPE.md`、`inference/STARTUP.md`、`TRAINING_RECIPE.md`。
4. `DataPrehandle/DAgger_V1_PLAN.md`及`Eval/artifacts/20261021-assisted-release-home/continuation-review/hil-readiness.json`。该JSON是旧设计清单，资源安排已被后续授权更新。
5. `DAgger/AGENTS.md`、`DAgger/docs/README.md`、`DAgger/docs/F1/STATUS.md`；进入其他目录再读其局部规则。需要H20时读infra/h20说明，并查实时进程。

### 2. 保持当前实验条件

- 暂用Full1000采集基线，复用已有H20推理服务；核验model_id/version、prompt、统计及processor，不凭端口猜模型。当前登记模型ID为`f1-toys-eef-efe3e5fa54ed0fb2`。
- 沿当前Full入口：D初值/下限14、S最小16并保留自适应，H50/30Hz、5步去噪、三图、ROI A、右EEF10、原始state16。当前客户端为`20261021-f1-toys-rtc-v6-param-read-r2`，如上线版本又变更，先说明差异。
- 新增显式、独立的RTC-HIL入口与冻结release/runtime，支持`--continuous`和可选时长，Ctrl+C完整收尾；到时/退出不额外发送Home。保留原continuous与回退入口，不冒充其已有HIL功能。
- 现有GPU0/1/3分别部署625/1K/1.5K，GPU2为独立IK，GPU4–7续训；这是快照，操作前查实时状态。本任务不停止这些服务、不另启训练、不无理由增加端口或改SSH权限。
- 不改IK限位、容差、模型、RTC算法、图像合同或平滑来掩盖接管问题。共享`cli.py/runtime.py`修改前核对并行改动，保留独立IK候选，不混入其可行前段恢复实验。

### 3. 从已有组件接入，避免重写整套状态机

以下路径均相对Projects：

| 位置 | 已有能力 / 要核对的缺口 |
|---|---|
| `DAgger/F1/src/f1dagger/rtc.py`、`standalone.py` | RTC继承Standalone；当前`operator_required=False`，`operator()`拒收人工输入，发布前检查也不允许正常HIL状态 |
| 同目录`cli.py` | 当前RTC仅在sync/sync-shadow分发，无IntentServer；continuous/时长也需接入新模式，不能只改脚本开关 |
| 同目录`policy.py` | 复用`F1PolicyEngine.operator/_operator/disconnect`，保留真实输入、新鲜度、重连和硬故障语义 |
| `DAgger/shared/src/dagger_core/{powered_hil_policy,hil_supervisor}.py` | 复用ChunkPolicy与HILSupervisor的接管/交还合同 |
| `DAgger/F1/src/f1dagger/{runtime,robot_io}.py` | `ControlLoop.step/reset_targets`、`F1Owner`，保持唯一发布owner |
| 同目录`recording.py` | F1Recorder/make_sink，完整`hil-record`与`purpose=policy_hil` |

优先考虑独立`rtc_hil.py / RTCHILPolicyEngine`加最小分发改动；可提出更好的实现，但必须满足下面不变量。

### 4. 控制权、请求与恢复必须一致

1. **真实授权。** 使用真实操作者连接及当前HIL的Enter/START约定：首次START授权策略，后续START接管；RB是人工运动deadman。核对实际按键合同，不伪造心跳，不用standalone本地恢复绕过操作者授权。
2. **接管立即生效。** 撤销旧chunk、EEF后缀和plan控制权，递增epoch；下一次发布前清掉策略待发目标，沿同一owner的实际状态安全接入人工，不能用末端远处的旧策略目标锚定。
3. **迟到结果没有控制权。** 在途网络请求不阻塞Control；旧epoch成功回复、IK异常、超时与hold/recovery回调全部作废，只留日志。人工期间不发新策略请求，旧deadline不能打断人工。
4. **夹爪和双臂来源明确。** 人工手臂/夹爪沿已有控制；绝对夹爪语义不变。废弃旧策略尚未执行的未来夹爪意图，但接管瞬间保留同一owner最近有效submitted的当前夹爪值及发布来源，再由明确人工操作改变，不能清零或强制开爪。左侧保持既有owner规则，不因模型未监督左侧而从policy执行左臂输出。
5. **交还重新观测。** 要求RB释放、人工夹爪动作完成及明确交还操作；等待交还边界之后的新三图和全部反馈字段，以当前状态重建anchor，清空旧`committed_actions16/previous_absolute_eef10`，bootstrap并重置延迟历史。
6. **故障路径可解释。** 保留已有IK/RTC失败保持、限位、碰撞、反馈新鲜度、operator watchdog、控制锁及断连保护；定义HIL模式的恢复授权。旧IK重试/Enter回调不能抢回控制，新包到来不能自动恢复运动。不以禁用保护或新增publisher实现接管。

### 5. 录制完整，专家标签不能混入错误策略动作

- 连续保存三图原件、时间戳及策略/HIL/保持过程。分别记录policy proposal、human candidate、selected、submitted、实际发布结果、measured；不能用反馈回填动作标签。
- 记录session、checkpoint/version、request/observation、epoch、intervention_id、接管/交还原因与输入序号、active_side、夹爪意图、plan撤销、迟到回复/回调丢弃原因和时间。保留阶段、布局、环境干预及结果的可审核标记。
- **错误状态保留；错误模型动作不自动作专家答案。** 仅审核通过的人工恢复`submitted`动作参与专家监督，不把删除坏段后的两侧拼接。模型成功段也不能默认重标成人工。
- 人手挪桶/扶物标为环境协助，不自动成为专家机器人动作；失败、未完成Home、来源不明或发布无效时保留边界。
- 记录人工释放→撤离→回位停稳，真实稳定后再留2–3秒。确认录制与导出不会在Home完成时提前截断。`human_hold`可能是有效终点保持，也可能是途中犹豫，必须按审核时间范围选择，不能全部纳入或全部删除。
- 原生Home单独记录来源、完成反馈和审核范围；不能把“发出Home”当“真实回稳”。本任务不复制末帧伪造真实保持。

### 6. 首条纠正必须能走到训练输入

- 复用`DataPrehandle/build_training_index.py`的`accepted_interventions`、`accepted_time_ranges_ns`和独立Home审核；观察有效性与标签连续性分开判断。
- H50有效前缀L=1–50与时间mask已实现，训练96窗口门槛已取消，直接复用并验证。来源/epoch/发布/时间缺口处截断，不跨段补标签；padding不参与动作噪声、attention、loss或统计。
- `scripts/prepare_formal.py:validation_indices`仍有验证每条96个互异窗口的假设。本轮要支持短HIL作为独立留出：采用明确的真实可变窗口数/分布式聚合，记录分母及阶段，不复制凑96，也不移进训练规避错误；原V1验证清单不悄悄重抽。
- 原12/3划分保留，新增按原会话/场次分组；同一次纠正、复制样本或相关片段不能跨训练验证。
- 为首条数据输出来源/阶段/有效窗口/L分布、拒绝原因及末尾保持入索引的审计。提供原示教、新示教、纠正三类可追溯manifest及混采所需计数；比例和下一轮初始化留待数据审核，不启动训练。

### 7. 分层验收，不能跳过现场边界

1. **CPU和传输集成**：真实授权门槛、重复/迟到START、执行中接管、在途成功/失败迟到、IK恢复回调竞争、人工夹爪、交还新观测/空前缀、断连/重连、相机失效、发布前deadline到期、Ctrl+C落盘。测试单一owner和同一次发布前最后检查。
2. **受影响回归**：policy/runtime/http_handoff/input_recovery/transport_pause/rtc/standalone及独立IK候选相关测试。证明原入口与并行工作没有被误替换。
3. **真实相机＋真实F710零输出shadow**：启动→策略→接管→人工→交还→新RTC；核验零publisher/零发布、模型身份和完整journal。合成按键测试单列，不能冒充真实手柄验收。
4. **用户现场短接管**：先安全自由空间，再孔口恢复与撤离回位；检查交接无目标跳变或意外夹爪动作。机器人动作由用户现场执行，禁止SSH代按授权或启动运动。
5. **首条数据审计**：逐段核对状态保留、标签来源、旧epoch作废、短窗mask、边界和终点真实保持；reader实际读取。先通过这一条，再给批量采集建议。

### 8. 最终交付

- 可回退的独立release/config及身份校验，已完成/未验收列表、日志和证据位置。
- 完整三端命令：操作者sender、输入隧道、F1现场入口，注明机器/终端/工作目录；提供continuous与定时、零输出shadow，说明启动/接管/人工/交还/停止按键。复用已有服务，减少用户操作。
- 首条可训练数据的审计结果；如果等待现场输入，明确留下待执行命令与未验收环节，不宣布完成。
- 更新任务STATUS/TODO、DataPrehandle计划、INFERENCE_RECIPE/STARTUP及DAgger当前状态，各写其负责事实；Markdown开头先给人类两三段简明结论，原始证据进artifacts。不擅自commit/push。
