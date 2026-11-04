# F1积木任务：协作入口

这里约定如何接续F1积木任务，适用于本任务全树；遵循Projects根规则。人类日常看[任务首页](README.md)，Codex用本页找到本轮需要的上下文，不要求操作者读完实验明细。

当前进度只维护在[STATUS](STATUS.md)，训练与推理各有一份根recipe，完整现场命令只在[STARTUP](inference/STARTUP.md)。本页保存稳定规则，不再累计历次实验和GPU占用。

## 每轮怎样接上上下文

1. 先读本页与STATUS，核对**当前目标、已确认决定、验收边界、下一步**。新用户指令优先；历史计划、旧聊天、过期Whereweare摘要不能覆盖已验证的新事实。
2. 按下面路由读取本轮相关正文和必要代码；不强制通读所有recipe、报告或原始日志。操作服务时重新查health/进程/GPU，文档记录不等于实时在线。
3. 涉及兄弟目录时读其局部AGENTS。F1任务合同仍作为需求来源，但本页不代替DAgger、RLinf、F1-4242或infra自己的实现规则。
4. 交付前检查：对应正文是否更新、STATUS是否反映本轮影响、TODO是否仍准确、证据是否可定位。只更新有变化的文件，不为每次聊天重复生成一套文档。
5. 中途未完成时在STATUS留下可接续的目标、已做/未验收、证据位置、下一步和阻塞原因。多人并行分别更新负责部分，合并前重读最新文件，不用整页旧副本覆盖他人结果。

| 本轮问题 | 主要读取入口 | 需要时再查 |
|---|---|---|
| 训练、checkpoint、prompt | [TRAINING_RECIPE](TRAINING_RECIPE.md) | [V1_FullSFT本轮配方](V1_FullSFT.md)、training/experiments |
| 推理、RTC、延迟、平滑 | [INFERENCE_RECIPE](INFERENCE_RECIPE.md) | [现场命令](inference/STARTUP.md)、inference/experiments |
| 时间对齐、Home、窗口、图像 | [DataPrehandle SOP](DataPrehandle/README.md) | DataPrehandle/reports与对应配置/reader |
| 真机结果、选模、IK诊断 | [Eval结论](Eval/README.md) | 日期rollout/分析、原始telemetry |
| H20操作与同步 | [H20_TASK](H20_TASK.md) | Projects的infra/h20/{README,SYSTEM_PROFILE,ENVIRONMENT,MULTIUSER_AND_SETUP}.md及实时资源 |

## [F1_INFERENCE_CURRENT] 当前推理文档

[INFERENCE_RECIPE.md](INFERENCE_RECIPE.md)是该tag唯一主文档，维护各模型分支的框架、RTC参数、延迟口径、执行频率及平滑。不得把历史实验的候选当作当前默认；运行事实和命令分别链接STATUS、STARTUP。

## 控制版本与模型身份

遵循[Projects版本命名规则](../../AGENTS.md#版本命名与发布身份)。F1控制／推理框架对人的主名称使用“关键技术＋V整数”，例如 `F1 RTC＋局部RLT V<N>`；共同运行时的VLA、HIL、RLT能力沿用同一递增控制版本主线，不按修复问题、开发者或checkpoint另起计数。F1infra工作台独立编号，不能将其历史V31直接套给控制端。

装配登记及冻结manifest需能将控制V号映射到完整release身份；首次采用此规则先核对现有控制版本历史、候选与真实部署，再登记对照。保留既有冻结目录和原始记录，不能只改显示名称就声称部署了新版；候选、已部署与回滚分别报告，当前实际指向仍由STATUS及实时manifest／health确认。

汇报与终端同时分清 **控制技术＋V号、base／encoder身份、actor／critic的run与checkpoint／step**。连续RLT的新actor只改变模型身份；控制代码或合同改变才分配新的控制V号。一次局部任务记录实际锁定的actor身份，不用“最新actor”或控制V号代替。

**每次更新都必须让操作者在实际PowerShell启动窗口看清版本。** 本地启动器显示自身打包身份；机器人连接后显示经manifest核验的当前控制技术＋V号，每次进入RLT再显示该段实际采用的actor身份。不同组件分开标注，网页试听更新不能冒充机器人升级，候选不能冒充已部署，旧运行进程不能因磁盘软链更新而显示新版；无法获取的身份明确标为未确认。发布验收包含终端显示与真实运行身份一致，不能只更新文档中的版本号。

## F1网络优先级

2026-11-03用户明确指定：**所有本任务联网入口优先有线，Wi-Fi只作备用。** 机器人系统路由／DNS、Windows操作端、工作台生成命令及当前说明统一遵循；不为更换网络改模型身份、内部驱动接口或历史证据。实际地址与机器配置见[F1-09](../../F1/docs/connect/F1-09.md)，Windows一次性设置与备用入口见[STARTUP](inference/STARTUP.md#wired-preferred)。

链路备份不等于运动授权自动恢复，连接切换仍保留session／ACK／watchdog和原现场授权。无法远程操作的Windows配置明确标为待用户执行，不以更新源码或上传安装包冒充现场已安装。

## 推理终端的稳定约定

所有F1策略推理入口（同步、RTC、RTC-HIL、Full/LoRA及后续模型）必须接入同一简洁显示层：配置一次看清、正常状态原位刷新、异常立即显示、完整原始日志可追查。只能合并已明确识别的重复消息，未知警告/错误不能静默丢弃；原TTY输入、Enter/START授权、Ctrl+C和退出码不得改变。具体规则与调试开关统一见[推理recipe](INFERENCE_RECIPE.md#inference-console)。

普通推理录制在4242“真机rollout”按真实checkpoint自动分类，与LXL、DAgger同级；HIL独立分类；shadow禁止生成录制/逐帧journal/视频，只保留普通程序日志。新增模型同步登记目录身份，时间取真实日志、原件与审核身份不改；具体口径见推理recipe。

稳定框架的日常运行不要求先单独shadow，同合同换checkpoint也不机械重复；保留即时启动/运行检查。输入合同、RTC、通信或控制生命周期变化时做针对性零输出验收，具体矩阵见推理recipe；shadow不代替现场任务验证。

## 稳定推理底座与新增模型

新增同输入/动作合同checkpoint只增加模型登记和部署配置；不得默认复制新Python/Bash、硬编码模型choices或逐入口粘贴D/S、prompt和框架路径。共用入口/模板生成实例配置，旧命令可保留薄别名。模型名须区分实验与步数，不能把旧V1 1K别名静默改指新DAgger 1K。

稳定运行时包含已验收RTC、通信/图像优化、UI、录制和退出逻辑；H20/6000D性能预设、rollout/HIL授权语义及候选IK显式分开。新增功能先在共同开发源演进，经相应验收再发布固定版本，冻结历史只读；实际能力从health与manifest核对，不能仅信注册表标签。装配登记统一维护`configs/inference_runtime_f1_09.json`，由`scripts/inference_runtime.py`生成实例，`deploy_inference_runtime.py`定向发布；不再以旧stage_full*.py/一次性脚本作为新增模型模板。算法组件仍固定版本，机器人切换情况以STATUS为准；实现边界和验收标准见[推理recipe](INFERENCE_RECIPE.md#stable-inference-base)。

新增通用推理能力前，先核对固定RLinf版本与上游已有实现，明确直接复用、适配保留和独立实验的边界；不另造通用模型/调度/机器人框架。普通同合同ckpt登记无需重复调研。上游升级、完整VJP与approx切换必须独立验收，不能混入配置整理；详见推理recipe的上游审阅。

## 文档放哪里、给谁看

| 内容 | 唯一维护位置 | 人类阅读优先级 |
|---|---|---|
| 任务导航、各页面用途 | README | 找入口时看 |
| 当前结论、验收边界、下一步 | STATUS | 日常先看 |
| V1阶段结论与下一阶段重心 | V1_Summry | 一页复盘；标明快照时间，不代替STATUS |
| 待办、暂缓、负责人/启动条件 | TODO | 做下一步决定时看 |
| 当前做法及理由 | 两份根recipe；V1_FullSFT只记录这一轮运行配方 | 讨论训练/推理时看 |
| 可复用数据方法、原理讲解 | DataPrehandle；所属场景visualizations | 想理解时在Why&How看 |
| 同一版本/问题的结果汇总 | training/experiments、inference/experiments、DataPrehandle/reports、Eval | 先看报告开头，追证再看正文 |
| 逐请求日志、CSV/JSON、截图、哈希、测试回执 | 所属artifacts或H20 run | Codex按需读取，不要求人类逐项看 |

- 使用准确文件名`AGENTS.md`；不另建`agent.md`、`agents.md`或第二份Current/Latest正文。既有目录合理，优先整理入口，不移动模型、原件或冻结release。
- 每份新增/更新Markdown在标题与必要metadata后，先用2–3短段概括**结论、影响、下一步/阅读时机**；避免在导读堆测试计数、长路径、哈希和参数。流程清楚时用短Mermaid，详细参数/推导/证据后置。
- 同一问题多次试验合并到一份版本报告，更新其导读与结论；仅当问题、数据版本或评估协议改变时新建报告。机器输出不逐个包装成MD。
- 历史记录标注日期、模型和适用范围；被替代结论链接新正文，证据完整保留。STATUS不再追加历史全文；阶段事实链接日期报告。
- Why&How固定4243，catalog只索引本任务原件。新的人类入口、重要复盘和教学材料按“先看这里→配方与结论→原理→历史”登记；不把全部机器产物塞进阅读目录。
- 汇报只给本轮结论、必要验证与下一步，通常至多3个优先阅读链接。目录整理前原文留在[留证清单](artifacts/20261021-doc-review/originals.json)。

## 必须保持的实验与执行合同

- 2026-10-29用户最终确定网页分类为 **LXL／真机Rollout／HIL**，HIL下为 **人工示教／DAgger／RLT**，按启动身份判断。已授权并完成网页／来源标签整理，原始文件名与存放路径保持。跨用途复用通过有源身份的审核与派生清单表达；例如DAgger纠正用于RLT种子时仍保留DAgger来源，RLT中人工接管仍归RLT；分类不替代训练准入或专家动作审核。
- 使用RLinf `openpi_rlinf` PyTorch π0.5；Full与LoRA身份明确，不能静默切backend或混用统计/prompt。当前具体参数以训练recipe和冻结配置为准。
- 模型输入按版本绑定，不能将动作mask当作观测mask。旧冻结版本保留三图/state16；2026-10-21用户确定下一版只用右侧8维有效状态及主图/右腕图，左侧模型状态固定占位、左腕image_mask=false，详见[单臂修正合同](TRAINING_RECIPE.md#right-only-input-fix)。不另做保留左侧信息的训练A/B，但必须验收隔离与实际任务效果。
- 主图固定ROI、有效腕图全图，共享确定性处理；训练随机增强只有任务adapter一个所有者，验证/推理无随机增强。相机或处理变化重做真实输入图核验；数据包、处理器和checkpoint不得跨输入版本混用。
- 学习右EEF10，固定观测锚点、rotation6D取前两行、绝对夹爪；原始物理state保留16D语义，模型状态另经单臂adapter处理。无效动作维、时间padding在对应输入/attention/loss/统计里一致处理，不能把旧关节8维动作合同套入新模型，也不能用模型占位值伪造硬件反馈或目标。
- 原生DH/FK与已验收的受限连续IK绑定同一运动学；不重开Pink选型或套用旧任务演示容差。修改限位、目标、恢复或平滑属于显式实验，不把离线可解写成可安全上线。
- 原始数据与冻结运行版本只读；变更图像、动作、归一化、mask、监督范围或模型适配须新版本，保留来源、反变换与检查。按episode划分，统计不含验证或padding。
- 每次训练含预检/恢复必须W&B，并设置group/job type/tags；独立实验新run、恢复保持step连续。证据分清计划、实现、离线、shadow、真机，不能用低loss或IK通过代替任务成功。
- 自动回位属于完整任务目标；EEF回位不保证冗余关节逐角一致，单块示教不证明已学会连续多块。当前数据边界与未解决问题见SOP/TODO。
- 现场动作由用户通过既有单命令、Enter、时长/continuous入口执行；shadow必须零输出，不伪造手柄心跳。独立策略与原数采/HIL启动合同分开；不擅自commit/push，不覆盖他人修改。

2026-10-29用户明确要求：F1局部RLT入口取消Enter等待，以真实F710 START为唯一开始授权；保留控制锁、反馈／通信／碰撞保护，不远程伪造START。其他路线沿用各自授权合同。
