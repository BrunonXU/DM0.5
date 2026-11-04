# 审计产物

本目录保留泡沫任务的来源、标签、数值评测与历史结论。2026-10-23用户确认旧派生数据不再使用后，已清理51.64 GiB reader图像和灰度帧缓存，剩余约7.67 GiB；代码、清单、历史视频与小型诊断结果保留。

下列旧验收结论仍按当时版本理解，不能据此直接运行缺失图像的reader。需要恢复时先读[当前状态](../docs/training_plan.md)；删除范围与核对结果见[清理回执](../../../reports/storage-audit-20261023/cleanup/foam/receipt.json)。

- 2026-10-23 [派生载荷清理清单与边界](../../../reports/storage-audit-20261023/cleanup/foam/README.md)：删除127,840张提取图像和1,555份灰度缓存，39,908个保留文件核验通过。

- 2026-10-14 [740 条 reader 原图清理回执](cache_cleanup_20261014/receipt.json)：用户暂停泡沫任务并授权清理；仅删除 736,049 个 `.image` 文件（150.25 GiB），清单与原始 MCAP 保留。外挂盘冻结数据的反向图像链接失效，恢复前须重建并读回；当前方案记录恢复条件。

- 2026-09-18 [左臂恢复v2发布器修复](deployments/20260918-4F-left-control-v2/README.md)：v1误用仅底盘后端，补齐双臂保持发布器；4测试和实机无发送端点检查通过，无需因该软件错误重启。

- 2026-09-18 [左臂模式恢复入口](deployments/20260918-4F-left-control-v1/README.md)：跟随等待后仍不动；新增现场退出拖动/位置模式→原home入口，未清故障，真实恢复待验。

- 2026-09-18 [home跟随等待v3](deployments/20260918-4F-home-follow-v3/README.md)：5°超差先暂停目标推进、等待跟随后继续，55测试、19文件部署；原home坐标姿态保留。

- 2026-09-18 [4F-F1 home左臂不跟随](home_tracking_20260918/README.md)：210目标/约7秒，右臂跟随、左臂七关节反馈变化均不足0.00008°；未将真实不响应误当5°容差过严，驱动原因待现场核对。

- 2026-09-18 [25K左臂IK拒绝与恢复](ik_rejection_20260918/README.md)：第5段step4残差2.97mm、候选跳变12.59°；新增有界重采入口，真实shadow持续拒绝，未宣称闭环恢复。

- 2026-09-18 [4F-F1示教192回放](deployments/20260918-4F-replay-192-v1/README.md)：复用已有完整53秒回放、192起姿与三路录包，作为模型推理的接口/数据对照。

- 2026-09-18 [25K 20/50无停稳等待](deployments/20260918-4F-raw20-25k-v1/README.md)：新增入口部署，34测试及两段实机shadow通过；保留50/50与旧版。
- 2026-09-18 [三checkpoint H50框架](deployments/20260918-4F-native-h50-v1/README.md)：原始50/50和异步接续分开；42文件部署、三模型无动作验证及录包通过。

- 2026-09-18 [4F-F1 home被充电状态阻止](home_status_20260918/README.md)：6次实读is_charging=true，其他状态正常，无急停/任务/告警；未转向或重采home。

- 2026-09-18 [4F-F1右转3°后采集home与25K](deployments/20260918-4F-home-right3-v1/README.md)：脚本部署；重启后SSH/模型转发恢复，待现场启动驱动和执行；三路短录包已在重启前验证。

- 2026-09-18 [4F-F1新home与双臂归位](deployments/20260918-4F-home-v2/README.md)：19文件、50测试、实机只读通过；保存新位置和双臂，目标回到指定朝向，待现场动作。
- 2026-09-18 [4F-F1临时右移50cm](deployments/20260918-4F-recover-right50-v2/README.md)：现场三阶段完成；留下约1°朝向残差，后由home-v2处理。

- 2026-09-18 [4F-F1 home朝向修复](deployments/20260918-4F-home-yaw-v1/README.md)：19文件部署，保留原home；32项测试通过，现场驱动缺失待恢复。

- 2026-09-18 [4F-F1 v4录包与25K接入](deployments/20260918-4F-F1-v4/README.md)：消息包版本冲突修复、18topic实录、三checkpoint预检与本机复位起点。

- 2026-09-18 [4F-F1补迁移192起姿](deployments/20260918-pose-192-4f-v1/README.md)：独立入口已部署，目标机缺HAL关节反馈，等待现场底层恢复。

- 2026-09-18 [4F-F1姿态与数据来源](4f_pose_review_20260918/README.md)：工厂试产机来源、本机参考绑定、原复位脚本与未启用的旧机消抖候选；[v3部署](deployments/20260918-4F-F1-v3/README.md)。

- 2026-09-18 [5K/20K双Docker同步推理](inference_ab_5k_20k_20260918/README.md)：GPU1双服务、同图同seed实测、完整Pass A对比、F1独立部署与预检、现场命令。
- 2026-09-18 [推理框架与合同审计](inference_framework_20260918/README.md)：真实processor/HTTP/冻结IK接线测试、客户端故障清理、20K输出范围与身体参考；checkpoint未选、未执行真机。
- 2026-09-17 [772条四卡100K训练启动](formal_training_20260917/README.md)：冻结数据/源码绑定、W&B、启动前验证和恢复入口；运行状态见当前方案。
- 2026-09-17 [772条最终部分底盘监督版](partial_supervision_pool_20260917_v2/README.md)：恢复上肢窗口、自动验收与冻结入口。
- 2026-09-17 [020/096/093抽样验收](replay_sampling_20260917_v1/log_review/README.md)：完整执行及停车协同、瞬时误差和demo限制。

- [transition_retention_20260917_v1/](transition_retention_20260917_v1/README.md)：全772条双臂转换归因、8条补帧及边界上下文修复；分别形成+280/+4,521窗口候选，主要转换缺口仍在，未改loss或替换reader。

该目录保存可重新生成的 MCAP/元数据审计、筛选清单和图像预览；不进入 Git，
本 README 除外。原始数据保留在 NVMe 数据盘，通过任务配置引用。
筛选清单不等于已经通过训练或真机验收。

- [full_pool_reader_20260917_v1/](full_pool_reader_20260917_v1/README.md)：740来源的state19/EEF23输入、248,906个H50窗口及28条原样对照绑定；全量reader/原图审计入口，当前进度见训练方案，不代表已冻结。

- [partial_phase_pool_20260917_v1/](partial_phase_pool_20260917_v1/README.md)：772来源逐阶段局部修复，740条产生244,862个H50窗口，来源/mask独立检查通过；32条零窗口保留，不把局部缺口当整条坏数据。

- [right_arm_supervision_discussion_20260917/](right_arm_supervision_discussion_20260917/README.md)：实际reader监督中的右臂活动、连续变姿缺口与恒定右夹爪统计；仅讨论，不改loss或mask。

- [replay_review_192_20260917/](replay_review_192_20260917/README.md)：192完整执行，左臂与原示教HAL逐帧对照，抓放区间实际零速；作为回归基线，不批准全量训练。

- [base_home_state_20260917/](base_home_state_20260917/README.md)：capture被server充电标志阻止，当前无急停/忙碌/任务标志；旧汇总报错未包含充电原因。

- [full_pool_scope_correction_20260917/](full_pool_scope_correction_20260917/README.md)：撤回28条最终规模，恢复772条全量处理；复核整条边界门和事件门造成的数据损失，建立逐来源局部修复清单。

- [demo_training_release_20260917_v1/](demo_training_release_20260917_v1/README.md)：28条对照来源、34,397个H50窗口的全原图/数值离线验收，train-only min/max修复与同源192现场包；后续192已完成回放，本子集不代表最终训练规模。

- [validation_infra_20260917/](validation_infra_20260917/README.md)：复用DM0.5流程的F1验证迁移收口；批量评估/续跑、复核图及决策留档，CPU保存/恢复、未训练LoRA独立重载与原NCCL脚本四卡检查通过。

- [validation_migration_20260917/](validation_migration_20260917/README.md)：100K配置、两阶段验证、episode配对比较及80/10/10候选排名迁移；训练loss不变，无正式训练或已训练checkpoint比较。

- [offline_repair_003_058_20260917/](offline_repair_003_058_20260917/README.md)：058局部监督隔离及H50防跨界，003符合动态限制的独立回放候选；未实机/正式训练验收。
- [pose_start_intersection_20260917/](pose_start_intersection_20260917/README.md)：修复020/096固定HAL起姿被误拒绝；选择原容差与硬限位交集，数据和阈值不变。
- [replay_review_20260917_053454/](replay_review_20260917_053454/README.md)：003完整执行和局部归位到位，松爪时底盘仍非零、双臂快段瞬时跟踪差待复核，协同未验收。
- [base_home_delivery_20260917/](base_home_delivery_20260917/README.md)：里程计起点保存/底盘平移归位脚本和5条独立诊断入口；已部署与只读检查，归位运动未实测。
- [replay_review_20260917_045324/](replay_review_20260917_045324/README.md)：057旧28秒执行完整，但第一次后退约8.4cm、松爪时底盘仍约0.139m/s，协同未通过。

- [migration_audit_20260917/](migration_audit_20260917/README.md)：实际远端入口、15个release盘点、两版replay逐文件比对及726文件定点取回；不改运行入口，大型rosbag与整机恢复另计。

- [gpu_wandb_setup_20260917/](gpu_wandb_setup_20260917/README.md)：用户授权停止G2两推理服务、释放GPU3/7，记录其他GPU占用变化；启用F1训练W&B，未启动正式训练。
- [dm05_integration_20260917/](dm05_integration_20260917/README.md)：既定recipe配置迁移、CPU Trainer/DataLoader与单卡模型前向/反向检查、24个标签→IK前缀通过；无optimizer更新或正式训练。
- [kinematics_validation_20260916/](kinematics_validation_20260916/README.md)：8份候选的原生FK/IK数值验证、来源绑定FK缓存与连续IK缺口；没有发送机器人动作。
- [continuous_ik_20260916/](continuous_ik_20260916/README.md)：改进DH模型与原生FK对齐；受限连续IK、action23前缀与确定性离线验证，F1运行验收另计。
- [ik_fk_demo_freeze_20260916/](ik_fk_demo_freeze_20260916/README.md)：`f1_ikfk_demo_v1`源码、F1参数与证据哈希冻结；接受已知缺口用于demo，暂缓算法优化和扩测。
- [ik_coverage64_20260916/](ik_coverage64_20260916/README.md)：新增64条、146,600次固定求解器扩测；发现76次分支失败与8个连续性复核标记，不能以原8条通过替代整体验收。
- [reader_validation_20260916/](reader_validation_20260916/report.json)：首版state19/action23 reader真实小批编解码、mask/统计与三图抽样检查；不是正式训练数据批准。
处理脚本见[F1管线](../docs/data_cleaning/f1_data_pipeline.md)，踩坑与解决依据见[清洗决策记录](../docs/data_cleaning/cleaning_decisions.md)，当前完成度见[当前方案](../docs/training_plan.md)。

## 内容

- [live_feedback_20260917/](live_feedback_20260917/README.md)：现场只读复现底盘缺反馈，定位igb网卡Tx Unit Hang；左腕压缩图像缺失、057起姿未就绪。无动作、未重启服务。

- [temporal_acceptance_20260917/](temporal_acceptance_20260917/README.md)：按用户demo时序优先决定核对8条已有阶段与底盘标签，3453停车监督帧非零冲突0；不代表新增画面语义验收，精确测距不再阻塞清洗。

- [reader_full_images_20260916/](reader_full_images_20260916/README.md)：现有8条候选全有效窗口原图提取与独立reader读回；8,898窗口、26,249张不同来源索引图，保留时钟异常帧隔离及候选状态。

- [`ftp_inventory_20260911/`](ftp_inventory_20260911/report.md)：09-11 FTP 初始盘点、清单与导入资源记录，2026-09-14 从工作区根目录 `results/` 原样迁入。
- [`20260914/`](20260914/)：下载完成后的数据审查、相机时间戳、筛选清单、预览和视频。
- [`cleaning_v1/`](cleaning_v1/summary.json)：全部801份录制的数值流、全量相机时间索引、30Hz对齐候选包及验证结果；处理方法见[清洗记录](../docs/archive/first_alignment_run.md)，尚不等于完成任务质量验收的训练集。
- [`cleaning_v2_review/`](cleaning_v2_review/annotation_guide.md)：逐循环标注和逻辑切片准备，801份来源、冻结分组、待审区间、验证与两条重点完整三路预览；待审不等于训练验收。
- [`demo_788_check/`](demo_788_check/report.md)：排除13份后完成788份数值/底盘、相机时间轴、重复与7092张图像抽检；发现全批末端监督脱节、000256底盘阶段错标强证据及9份头部时钟簇。这是处理前的历史审计；后续排除与mask见demo_778_tail。
- [`demo_778_tail/`](demo_778_tail/report.md)：再排1份底盘错标与9份头部时钟簇，保留778份；逐条保守尾段mask及50步边界已验证，剩784454个候选动作块；两例三路尾段曲线和1例25.7秒数值dry-run完成。708份因此缺少部分返程，真机及训练reader尚未验收。
- [`demo_775_selection/`](demo_775_selection/report.md)：回放视觉复核后再隔离083/066/002，剩775份；数值dry-run不等于真实标签验收，三份均有底盘标签提前归零而背景继续移动的证据，当前完整联合真机replay暂缓。
- [`demo_775_phase_review/`](demo_775_phase_review/report.md)：09-15完成775份上半身复核、启动mask、原MCAP背景图片/头身体反馈提取、全量阶段筛查与三批原图专项确认；自动疑点与确认排除分开记录。
- [`demo_772_selection/`](demo_772_selection/report.md)：再隔离三份原图确认底盘阶段错标来源，保留772份及当前mask/质量清单；来源候选仍需语义复核，未放行训练或真机。
- [`base_anchor_review_20260915/`](base_anchor_review_20260915/anchor_analysis.md)：六条手/夹爪锚点、原底盘窗口与背景疑点对照；[补标配方与根因层级检查](base_anchor_review_20260915/recipe_audit/findings.md)及新版[底盘审核页](20260914/videos/base_review/index.html)。HAL重建暂停，未修改底盘原标签。
- [`base_script_audit_20260915/`](base_script_audit_20260915/report.md)：用户提供源码后的窗口/六段机制审查、8项反例/静态检查、775份原配方对应、000057截图待澄清草稿与修复验收方案；原标签和冻结清单未变。
- [`base_relabel_v1/`](base_relabel_v1/README.md)：显式逐段重标、六条视觉候选、来源绑定、31项工程测试和浏览器导出协议验证；真实000057另存五段移动与停顿提议、新旧曲线及65项独立核验，尚未恢复训练数据。
- [`interface_review_20260914/`](interface_review_20260914/documented_range_audit.md)：新增接口文档的双臂限位、夹爪范围与801条信号包对照；仅报告差异，未裁剪或修改原清洗mask。
- [F1远端候选代码位置](interface_review_20260914/remote_code_inventory.md)：隧道接通后的只读定位，列出主从桥接、安装配置与相机代码；尚未证明它们是采集时或当前运行的版本。

09-11 是上传期间的历史快照；其中下载服务的运行状态、数据总数和资源占用不代表现在。
最终 801 条数据及停止同步的记录见[任务说明](../README.md)和[09-14 数据审查](../docs/archive/raw_dataset_audit.md)。

- [task_boundary_distribution_v1/](task_boundary_distribution_v1/report.md)：现场距离确认后的772份首尾信号与背景分布；夹爪闭合次数不当循环数，末端未知不当停车。
- [task_boundary_visual_v1/](task_boundary_visual_v1/report.md)：跨四批5条代表末段视觉复核，检查返回、收臂与是否存在末段前进；非随机抽样，不能推广成772条统一五段。
- [task_boundary_ui_v1/](task_boundary_ui_v1/report.json)：针对截图2秒重叠输入的实时提示、确认/导出拒绝、草稿保持和移动端检查。

- [docs_reorganization_20260915/](docs_reorganization_20260915/README.md)：文档按职责迁移的路径表、原文备份与链接校验；未改动数据清单或派生标签。
- [reset_exclusion_scope_20260915/](reset_exclusion_scope_20260915/README.md)：用户允许首版不学复位后沿用尾段mask；既有名单取交集形成67份底盘优先审核队列，未新增视觉或训练验收。
- [base_relabel_v2_000057/](base_relabel_v2_000057/README.md)：收到用户草稿后按原图修正返程停顿、保持28.1秒尾mask，生成底盘sidecar和19D候选包；727联合有效帧、296窗口，5项工具测试及16项独立检查通过，未放行训练或完整replay。

- [cleaning_resume_20260916/](cleaning_resume_20260916/README.md)：replay后恢复清洗，772份清单/草稿/背景证据身份核对、尾界盲区诊断及跨四批24条待审队列；没有新增训练批准。
- [replay_review_20260916_051230/](replay_review_20260916_051230/README.md)：两轮057真机回放对照及用户现场成功观察；问题统一见[replay故障复盘](../docs/incidents/replay_failures_and_lessons.md)。

- [replay_distance_audit_20260916/](replay_distance_audit_20260916/README.md)：057分腿指令/反馈积分与速度先验审查；现场位移不足，冻结批量扩展，先完成8条小批验证。

- [base_response_offline_20260916/](base_response_offline_20260916/README.md)：逐段响应审计、8条动态要求、待部署可选驱动诊断录制；107项测试通过，物理位移修复仍待控制器证据和现场验证。

- [4F-F1 home会话有效期v4](deployments/20260918-4F-home-session-v4/README.md)：88测试、19文件已部署并备份校验；同次开机取消一小时过期，跨开机单独恢复双臂。只读规划当前缺arm_l_j1反馈，待系统启动和现场验证。

- [4F-F1 192分段起姿](deployments/20260918-4F-pose-192-segment-v1/README.md)：已部署、63测试、20文件核验；只读32段规划通过，真实复位待现场。
