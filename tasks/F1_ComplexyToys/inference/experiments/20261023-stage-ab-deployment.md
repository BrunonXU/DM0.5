# A1250／B1250与旧Full1K：同框架真机对照部署

**A/B已完成首批现场比较，旧Full1K现已恢复供同条件对照。** 用户感觉B稍好，但本批未确认完整任务成功，实际结果见[真机复盘](../../Eval/V1_DAgger/README.md)。旧Full与新模型输入、统计及训练来源不同，不能只归因于DAgger。

最终服务为A、B、旧Full1K与DAgger1K；旧Full接替备用625，复用其GPU及端口。模型各自的原图像处理和统计不混用，共享RTC、IK、终端与录制底座。恢复Full的缺失输入兼容已通过真实相机零输出检查，没有远程启动机器人动作；命令见[STARTUP](../STARTUP.md)。

## 旧Full1K恢复（同日14:12，北京时间）

- 原bundle仍为`f1-toys-v1-full-20261020-r1/bundle-1000`，model ID为`f1-toys-eef-efe3e5fa54ed0fb2`；原三图/state16、processor、归一化、RLinf core与IK保持。复用GPU2、H20 8160→F1 8155，不新增SSH权限。
- 可选服务profile `legacy_left_gripper_zero_v1`明确区分缺失设备与模型补值：启动绑定原训练manifest并验证13,102个锚点左爪全0；每请求只允许该槽缺失、原占位0、时间戳0。补值只作用于模型输入副本，原physical state、held与FK不变；原件缺失mask继续保留，左爪无publisher。拆后视觉外观仍不同，工程兼容不证明旧策略性能恢复。
- 新服务release `f1-toys-20261023-full-restore-r1`复制旧冻结版本，仅修改server接线、新增profile模块；旧release不改。没有为checkpoint另写推理框架。新统一装配`unified-ab-full-20261023-r1`，H20旧Full机器配置装配同名加`-legacy`，机器人公共入口已原子切换。
- profile CPU完整22项在H20隔离环境通过；本机共享装配＋profile为56通过、4项因缺Torch/aiohttp跳过，这4项由前述H20完整测试覆盖。首次本机未设置PYTHONPATH导致收集失败，设置后发现旧活动清单断言过时；历史断言已绑定对应冻结清单，并新增恢复Full合同隔离检查。
- 5秒真实相机shadow：正常退出、9/9请求成功；请求replay的缺失mask/时间戳与profile一致，状态末次观测ID144，零输出模式无command publishers，无新增录制。只用于输入兼容验收，不证明插入成功。
- 切换只停止确认uid与bundle的备用DAgger625进程；A/B及DAgger1K没有重启。五种model/mode `--check`通过，父1K HIL默认不变。4242已登记旧Full模型身份，本次无需修改页面。

[恢复产物](../../artifacts/20261023-full1k-restore)包含旧catalog/真实入口、完整新装配、源码manifest、服务切换、health、shadow、replay与原子切换回执。回退需在空闲控制锁下恢复625服务和上一装配，不能只改别名；模型权重未复制或删除。

## A/B首次部署快照（同日上午）

以下保留上午部署事实；其中625备用已被上面的旧Full1K替换。

## 绑定与验证

| 候选 | 别名 | GPU | H20端口／F1端口 | 模型身份 |
|---|---|---:|---|---|
| A：原采样1250 | stagea1250 | 0 | 8137／8152 | f1-toys-eef-ede2bbb7c49d30ed |
| B：释放加权1250 | stageb1250 | 1 | 8138／8153 | f1-toys-eef-81444e7071723c1f |

两端装配为`unified-stage-ab-20261023-r1`；服务器/处理器、客户端仍固定既有release，D14/S≥16、H50/30Hz、5次去噪、standard IK。无需新增SSH权限。旧250／500标签保留历史归档，但活动别名不再可用，避免静默转指新模型。

- H20四个服务真实health与完整模型合同通过；F1四个rollout及父1K HIL共五个`--check`通过，处理器和客户端哈希逐项验证。
- 只停止确认为本任务、本人账号的旧250／500 PID；服务切换时持有空闲控制锁，未停止基础驱动、相机或其他工作。
- 4242从实际线上release复制，只增加两个模型标签。401个文件比对仅注册表变化；原105条录制身份/审核一致，HIL路由字节不变。
- 本次同合同换权重未重复shadow；工程核验不等于真机任务通过。

## 留证与回退

[部署产物](../../artifacts/20261023-stage-ab-deployment)保存旧catalog、真实入口快照、模型清单、服务切换、两端stage/activate、4242注册表及备份。H20/F1均保留前装配和入口备份；若需要恢复旧250／500，必须同时恢复对应模型服务与旧目录身份，在空闲锁下进行，不能仅改别名。

原训练与离线比较见[阶段采样复盘](../../training/experiments/20261022-stage-sampling.md)。
