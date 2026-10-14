# 首轮真实Powered HIL数据流核查与训练时间处理

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

对象：`g2_powered_hil_20260907_155216`。本轮只读拉取并审计已经完成的运行，不启动真机，不修改原始记录或放宽审计。
证据：`logs/offline/20260907_powered_hil_run/` 下原始journal、telemetry、`audit.json`、`flow_summary.json`。

## 实际跑通的部分

- journal完整封口，769组三路观测、16个policy chunk、7532条control、14014条events；journal及主遥测drop=0、writer_error=null。
- 16个推理结果中15个accepted、1个拒收；4次进入人工、3次HANDOFF后恢复policy。控制policy引用的epoch/request及response-before-control独立核查未发现错误。
- 三次退出等待分别230.343、296.097、312.263ms；对应新观测在handoff后12.768、41.142、82.643ms开始，均满足handoff < observation <= request <= response <= acceptance。
- 请求到响应中位219.664ms，最大263.845ms。这是客户端调用整体时延，不是纯GPU forward，也不是动作执行步长。
- 控制行间隔中位10.000ms、P99 12.976ms、最大24.366ms；POLICY和HIL各自中位都约10ms。HIL全程speed_scale=1.0；START不降低Servo目标频率，切换为人工低速参考轨迹。
- 观测间隔中位100.192ms、最大108.212ms，HIL内487组、HANDOFF内9组，持续采集成立。
- 10275次实际SDK调用（10244 Servo、31手点目标）均记录返回0；这证明提交返回成功，不证明逐tick实测轨迹。
- payload审计通过：769组三路原始帧及索引、哈希/时间推进检查未报错；host接收偏斜最大1.692ms。未做视频完整解码/编码认证，也未映射设备时钟，不能当曝光同步误差。

## 发现的问题与限制

1. **完整训练/跨流审计未通过，training_ready=false。** 不能因为动作跑通或原始帧哈希通过就投入训练。
2. 7532条“invalid control policy reference”主要入口之一是SDK记录字段不匹配：审计期待finished_monotonic_ns/interface/joint_indices/submitted_positions，而40保存completed_monotonic_ns/api/joint_names/joint_positions。错误名具有误导性，独立检查policy epoch/request/time无错误；应规范producer与auditor合同，不能直接忽略所有错误。
3. fresh恢复事件缺审计期待的policy_request_id；可由同epoch真实result关联复核，但需补显式引用。policy_proposal当前是插值输出，审计期望原waypoint，需分别记录raw waypoint、插值系数和selected。
4. 652条Home不在旧审计source枚举内；人工同时控臂/手时source只写human_arm但hand mask为true。Home及人工hold存在selected/source不一致报告（合计502条），需要逐类核对标签与实际SDK维度，不能批量宣布只是旧审计误报。
5. POLICY状态等待下一chunk时source=hold，旧审计只允许HANDOFF/SAFE下hold，因此341条state/source不匹配；需要显式区分sync policy_wait与其他hold。
6. 有一次输入包龄206.229ms触发人工dispatch watchdog_hold（packet646）。没有观测到同等大小的控制行间断，不能据此说整轮控制变慢；仍需保留这次断续事件用于样本排除/诊断。
7. console中13秒dispatch_interval最大值不是全链SDK停止13秒：该统计只在人工分支维护，跨越policy阶段后计算出伪长间隔。全链control相邻最大24.366ms。后续应修复候选诊断统计，不能用该console值证明大停顿。
8. reported手20D仍valid=false；物理手部到位、训练标签和实际measured对齐没有完成。

## START与后处理建议（尚未实施数据裁剪）

保留START为显式执行所有权边界，记录为元数据，不必把按钮作为普通策略输入。人工选择介入边界符合[HG-DAgger](https://arxiv.org/abs/1810.02890)的human-gated交互方式；具体训练采样仍按本项目合同确定。

慢示范不是自动无效：准确纠正有价值；但大量犹豫/等待若直接作为专家目标，可能让策略学会停顿。当前命令动作合同为30Hz，模型推理更快不改变这个时间语义。

建议先保留不可变原始记录，在派生训练集执行：

- START接管前上下文保留；等待操作者选侧、无任务意义停顿、policy等待、Home、网络异常分别标注。不能只靠零速度判断，因为等待接触稳定/保持抓取可能是有效任务动作。
- 有效人工纠正作为候选监督；策略自动轨迹不直接冒充专家，未人工控制维度与hold标签需明确策略。
- 裁去无意义等待时分成独立连续片段，或屏蔽跨裁剪边界的50步训练窗口；不要把相隔数秒的帧硬拼成一个30Hz连续动作chunk。
- 如做时间压缩，需对三路图像、state、action采用同一个时间映射，并按30Hz合同重新采样。只加速视频或只抽action会破坏关联。压缩还改变速度/加速度和接触动态，须对派生样本单独验证；默认先裁等候，不整段强制2倍速。
- 本轮按control区间累积的human_hold约12.724秒、Home约6.624秒。这些是候选分类时长，不代表已确认都可删除。

下一优先级是修正记录字段/标签并完成严格审计，而不是取消START或盲目加速已有数据。本轮没有更改冻结F710、40运动参数、START规则或训练数据。
