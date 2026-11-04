# F1 积木任务：π0.5 环境版本决策

日期：2026-10-15（Asia/Shanghai）。状态：环境搭建候选；实际安装、解析锁文件和 smoke 结果以关联部署记录为准。

此目录只定义 `F1_ComplexyToys` 首版准备环境。6000D 保留 Codex、开发与推理，H20 安装个人训练环境和保存数据。这里不改变其他人的账号、全局 Python、驱动或系统 CUDA。

## 已确定与尚未确定

- 任务文档确定 RLinf / π0.5 SFT，先约 10 条审核示教，再扩展；头部主视角与右腕输入，左腕 image mask；F1 外部状态/动作保留 16D 绝对目标，默认 H50、30Hz，另做20Hz对照。
- 本次为准备环境暂定 `model_type=openpi_rlinf`、`openpi.task=sft`；这是基础设施选择，不是已验收的 F1 训练配置。内核模型32D、Gemma2B+300M expert、token budget200，可承载所需形状，但具体适配仍须验收。
- ROI坐标、人工成功审核、F1专用reader/adapter、归一化统计、真实训练batch尚未完成；现有候选仍为 `training_ready=false`。不能拿模拟器/Franka/BEHAVIOR现成配置替代F1，也不在本次准备中启动正式训练。

## 来源快照

| 来源 | 本轮读取的身份 | 使用方式 |
|---|---|---|
| `RLinf` | `d00fe72060a5aa1554e92fe7bf72846d43d48c1c` | 复制明确源文件快照，记录未提交差异后 `--no-deps` 安装 |
| `PI0.5` | `15a9616a00943ada6c20a0f158e3adb39df2ccac` | 参考官方base来源、数值依赖锁，不与RLinf fork混装 |
| `third_party/lerobot` | `713a409faedd73bb5597481b8885f17fbee23330`，0.6.2 | v3数据工具参考；不得直接装入本环境 |
| PyPI `lerobot` | 训练环境0.3.3；独立v3数据环境候选0.4.3 | 传递依赖检查后明确拆分；不混装NumPy1/2依赖 |
| PyPI `rlinf-openpi` | 0.1.1，metadata及wheel导入路径已只读核查 | 提供OpenPI变换、loader和JAX兼容层 |
| PyPI `rlinf-transformer-openpi` | 4.53.2 | 唯一 `transformers` import包提供者；不同时安装stock transformers |

Git HEAD不代表工作区完全无改动；部署清单必须记录实际文件哈希。

## 关键版本与理由

1. Python暂定3.11.14（RLinf安装器默认），PyTorch2.7.1 + torchvision0.22.1，GPU wheel明确选择cu128。H20驱动570.133.20报告CUDA12.8；该wheel自带运行时，不因此安装或更新系统驱动、nvcc。不要从PyPI直接拉当前最新版torch，它可能需要更新的CUDA主版本。
2. `rlinf-openpi==0.1.1` 元数据硬要求torch2.7.1、NumPy<2、jax[cuda12]0.5.3、Flax0.10.2、Orbax0.11.13。RLinf当前pyproject override默认torch2.11.0，与该包声明不同；本候选按OpenPI声明建立独立最小环境，不运行会改写依赖/安装仿真器的通用安装脚本。
3. RLinf FSDP入口对torch>=2.6使用公开FSDP2 API，对2.7的`ignored_params`有显式兼容检测。静态审计未发现必须2.11才能导入的本任务路径；仍需实际导入、GPU前后向及checkpoint恢复验证，不能将静态审计写成训练通过。
4. `rlinf-transformer-openpi==4.53.2` 要求HF Hub<1、tokenizers>=0.21,<0.22。本候选锁Hub0.35.3/tokenizers0.21.4；不增加会再次引入stock transformers的PEFT或其他VLA包。OpenPI_RLinf内核直接使用PyTorch attention，不要求flash-attn或本地CUDA编译器。
5. 数值栈沿用本地PI0.5锁中的关键兼容版本：NumPy1.26.4、equinox0.12.2、optax0.2.4、ml-dtypes0.4.1、tensorstore0.1.74。避免宽范围依赖升级JAX而使Orbax导入失败。
6. OpenPI元数据要求opencv-python，而LeRobot要求opencv-python-headless；本候选只能按两个元数据同时锁相同4.11.0.86，存在共用`cv2`命名空间，应在验收时检查实际导入。若后续改为单一headless提供者，必须另存经过审阅的包元数据修订，不能悄悄忽略依赖。

`requirements.in` 是可交给解析器的显式候选，不声称所有传递依赖已锁或已安装。实际解析后保存包含版本/来源/哈希的锁文件、pip freeze和安装检查结果；若解析器报告冲突应修正本候选并记录原因，不使用全局升级掩盖冲突。

## LeRobot v3方案与接口门槛

**最终训练环境保留 `lerobot==0.3.3` / datasets3.6.0 / NumPy1.26.4；当前F1 v3数据必须使用独立的数据环境或后续专门适配的reader。** 本轮曾在只核查直接依赖后提出0.4.3同环境方案，但实际uv解析揭示漏查的传递依赖：LeRobot0.4.0、0.4.1、0.4.3全部要求`rerun-sdk>=0.24,<0.27`，这一范围的rerun要求`numpy>=2`，与`rlinf-openpi==0.1.1`的`numpy<2`硬约束冲突。此前“同环境可读v3、不需第二环境”的结论已撤回；更换0.4.1不能解决问题。即使本任务不调用rerun，也不能安装旧rerun、隐藏metadata或忽略pip check来宣称依赖一致。

训练环境的0.3.3源码是`CODEBASE_VERSION="v2.1"`，**不能读取当前F1 v3原件**。它的`rerun-sdk>=0.21,<0.23`可用0.22.1，该版本仅要求NumPy>=1.23，存在Linux x86_64的cp38-abi3 wheel。PyAV选择14.2.0：其官方cp311-manylinux2014-x86_64 wheel可直接安装；14.3.0与14.4.0没有对应Linux cp311 wheel，实际14.4.0源码构建已失败，不能继续盲目重试。训练环境同时保持gymnasium0.29.1、pyarrow20.0.0、accelerate1.7.0、wandb0.21.4。

独立v3数据环境候选如下，不安装`rlinf-openpi`或它的transformer fork，最终仍须单独解析、锁定与验收：

| 组件 | 候选版本 | 依据 |
|---|---|---|
| LeRobot | 0.4.3 | 官方wheel `CODEBASE_VERSION="v3.0"`；Python>=3.10、torch>=2.2.1,<2.8、torchvision>=0.21,<0.23 |
| NumPy | >=2，按独立数据环境最终lock固定 | 满足rerun-sdk>=0.24；不可装入OpenPI训练环境 |
| datasets | 4.1.1 | 满足LeRobot>=4.0,<4.2；直接NumPy约束不是冲突源 |
| pyarrow | 21.0.0 | 满足datasets4.1.1的>=21要求 |
| accelerate | 1.10.1 | 满足LeRobot>=1.10,<2 |
| gymnasium | 1.1.1 | 满足LeRobot>=1.1.1,<2 |
| PyAV | 15.1.0 | 满足LeRobot>=15,<16；有cp311-manylinux_2_28-x86_64 wheel |
| wandb | >=0.24,<0.25，实际patch由最终锁固定 | 满足LeRobot0.4.3 |

0.4.3相较0.4.1包含按`episodes`子集读取时global index→relative index处理，对之后训练/验证划分有价值，但不能据此省略实际子集回读。独立数据环境可以核查和准备数据，**不代表已经打通v3到OpenPI SFT batch**；本轮不降级原始metadata，也不临时导出整个TB级v2数据副本来掩盖reader缺口。

RLinf pyproject中的`datasets==3.6.0`属于`[tool.uv].override-dependencies`，核心`[project].dependencies`只声明`datasets`，并非硬约束。因此datasets4.1.1本身不是违反RLinf元数据的问题；本轮放弃同环境v3是因为rerun→NumPy的真实传递冲突。独立数据环境可以合理使用4.1.1，训练环境按最终3.6.0锁维护。不得对任一环境运行会重新应用其他项目override的无条件`uv sync`。

`rlinf-openpi==0.1.1` 的loader先尝试 `lerobot.common.datasets.lerobot_dataset`，再回退 `lerobot.datasets.lerobot_dataset`，可匹配0.4.3的模块位置。RLinf本地 `rlinf/data/storage/lerobot/compat.py` 也明确支持>=0.4/v3的episode边界与写入签名。**这些不表示完整OpenPI SFT loader已经兼容v3。** 已核查的固定wheel存在具体接口缺口：

- `openpi/training/data_loader.py:create_torch_dataset` 把`dataset_meta.tasks`原样传给`PromptFromLeRobotTask`。
- `openpi/transforms.py:PromptFromLeRobotTask` 期待`dict[int, str]`，调用`self.tasks.get(task_index)`。
- LeRobot0.4.3的`meta.tasks`是Pandas DataFrame；`.get(int)`按列取值，不是按任务ID读取文本。其dataset样本已有正确的`task`文本。

F1业务reader/adapter阶段必须显式转换任务映射，或消费样本`task`字段，并验证非连续任务ID、实际任务文本与训练prompt一致。本轮不修改业务adapter或site-packages掩盖这个问题。完整训练入口在该缺口和F1数据合同验收前仍不能宣称可训练。

现有 `third_party/lerobot` 0.6.2要求Python>=3.12、NumPy>=2、HF Hub>=1.6、datasets>=4.8，仍不直接装入本环境。原有基于0.6.2的审核工具可继续留在其原环境；不因独立数据环境候选0.4.3而重写数据、降级metadata或覆盖先前报告。继续保留原始记录和独立动作索引。F1reader必须真正应用起点筛选与完整H步标签，而不是从所有LeRobot行重新随机抽样。

### 初始v3回读smoke

这一步仅检验环境和格式，使用一份已有、哈希可追溯的小型F1 v3候选副本；保持`training_ready=false`，不传整套TB级数据，不修改原件。历史可对照源是 `DAgger/artifacts/results/20261013-resampling-fix/f710_073059_resampling_candidate` 及同目录`reader_check.json`；H20副本路径与哈希必须以实际同步清单登记，不能预先写成已存在。

1. 在独立v3数据环境通过 `LeRobotDataset(repo_id="local/f1_upper16", root=实际副本目录, video_backend="pyav", delta_timestamps={"action": [i / 30 for i in range(50)]})` 读取；设置Hub/datasets离线模式，只使用本地副本。
2. 核对meta的v3.0、30Hz、episode/frame数量与源清单；解码首帧、中间和末帧的实际相机图，确认图像形状、RGB范围及有限值，state16/action16与原Parquet逐值一致。
3. 从足够长episode起点检查`action`为50×16、完整窗口`action_is_pad`全False，末帧检查后续padding标记及实际重复值；这些padding仅作读取检查，不授权其参与F1训练loss。
4. 另外指定至少两个非连续episode ID重读，核对索引、图像与动作仍对应原episode；这验证0.4.3子集索引路径，而不只检查全量顺序读取。
5. 显式记录`meta.tasks`类型、任务ID→文本与样本`task`的一致性；将OpenPI `PromptFromLeRobotTask`不兼容列为待修项目，不把直接LeRobot回读写成RLinf训练batch通过。
6. 保存解释器、库版本、输入哈希、抽查索引及逐项结果，复查原件哈希未变。此读取smoke不使用GPU、不启动Ray、不连接机器人。

## Base模型与转换

- 来源：官方 `gs://openpi-assets/checkpoints/pi05_base`。保留下载文件、上游对象信息与SHA256，不使用已经针对其他任务微调的权重冒充base。
- 转换器：`RLinf/rlinf/utils/ckpt_convertor/openpi/jax_to_openpi_rlinf.py`。它从`params/`读JAX/Orbax，输出OpenPI_RLinf裸Pi0键空间的fp32 `model.safetensors`。
- 权重转换使用CPU/JAX平台即可；不要让JAX默认为转换预占所有GPU。目标目录另建，不覆盖JAX原件。
- `openpi_rlinf`与OpenPI PyTorch的`paligemma_with_expert.*`键空间不同，不能只重命名文件加载。转换后核查全量键和形状，并用真实模型加载验证。
- Tokenizer来源为 `gs://big_vision/paligemma_tokenizer.model`；RLinf另提供 `RLinf/openpi_tokenizer`。`rlinf-openpi` wheel的tokenizer直接调用前者，所以须按实际 `openpi.shared.download` 缓存规则准备，不能仅把文件放到任意目录就宣称离线可用。
- Base不包含F1归一化统计；禁止借用其他机器人的norm_stats作为F1统计。

## 最小验收

1. 独立解释器、依赖锁、RLinf源文件hash；确认只有fork transformer提供`transformers`，导入版本/路径符合预期。
2. `torch` CUDA可用，bf16矩阵前向/反向有限；选择实测空闲GPU，短测后释放，不占满全部GPU。
3. 导入 `rlinf.config`、`rlinf.hybrid_engines.fsdp`、`rlinf.workers.sft.fsdp_vla_sft_worker`、`rlinf.models.embodiment.openpi_rlinf`、`openpi.training.data_loader`，以及真实使用的LeRobot/torchcodec视频解码路径。完整SFT入口需要Ray、Hydra、OmegaConf、torchdata、accelerate和transformer fork；单独torch导入通过不算训练栈通过。
4. 官方base完整下载并校验，CPU转换，真实模型载入；随后以合成观测执行一次有限loss/梯度及临时checkpoint保存恢复。这仅验收infra，不代表F1数据/任务训练或真机通过。
5. F1下一阶段另做数据合同、图像mask、16D↔32D往返、真实batch、完整窗口及人工审核验收。

## 证据路径

- 任务权威说明：[training/README.md](../../../../tasks/F1_ComplexyToys/training/README.md)。
- 本地实现：`RLinf/requirements/embodied/models/openpi.txt`、`RLinf/requirements/install.sh:install_openpi_model`、`RLinf/rlinf/hybrid_engines/fsdp/__init__.py`、`RLinf/rlinf/data/datasets/openpi_rlinf/official_sft_data_loader.py`。
- 官方包元数据：[rlinf-openpi 0.1.1](https://pypi.org/pypi/rlinf-openpi/0.1.1/json)、[transformer fork 4.53.2](https://pypi.org/pypi/rlinf-transformer-openpi/4.53.2/json)、[LeRobot 0.4.3](https://pypi.org/pypi/lerobot/0.4.3/json)、[datasets 4.1.1](https://pypi.org/pypi/datasets/4.1.1/json)、[accelerate 1.10.1](https://pypi.org/pypi/accelerate/1.10.1/json)、[PyAV 15.1.0](https://pypi.org/pypi/av/15.1.0/json)。
- 训练环境及传递依赖依据：[LeRobot 0.3.3](https://pypi.org/pypi/lerobot/0.3.3/json)；另核查过[0.4.0](https://pypi.org/pypi/lerobot/0.4.0/json)和[0.4.1](https://pypi.org/pypi/lerobot/0.4.1/json)的v3源码及元数据。

- 传递冲突与二进制证据：[rerun-sdk 0.22.1](https://pypi.org/pypi/rerun-sdk/0.22.1/json)、[rerun-sdk 0.24.0](https://pypi.org/pypi/rerun-sdk/0.24.0/json)、[rerun-sdk 0.26.2](https://pypi.org/pypi/rerun-sdk/0.26.2/json)、[PyAV 14.2.0](https://pypi.org/pypi/av/14.2.0/json)。
