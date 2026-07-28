# DM0.5 × G2：RTX 6000D服务器迁移与新对话交接

更新时间：2026-07-28

## 0. 本文的用途和权威性

本文是从本地RTX 4090D开发环境迁移到RTX 6000D训练服务器时，新Codex对话
的唯一交接入口。新对话必须先完整阅读本文，再按本文列出的顺序读取其他文档
和检查实际服务器。

如果旧聊天记录、旧状态描述或一般性建议与本文冲突，以以下顺序为准：

1. 用户在新对话中的明确指令；
2. `g2_sort/config/action_contract.json`；
3. 本文；
4. `docs/02_IMPLEMENTATION_STATUS.md`；
5. `docs/01_REPRODUCTION_PLAN.md`及其他项目文档；
6. 一般经验或历史聊天内容。

本文不记录服务器地址、用户名、密码、共享盘主机名、访问token或其他凭据。
这些信息只能由用户在实际环境中提供或由当前登录会话发现，不得提交Git。

## 1. 项目目标

本项目不复现LIBERO benchmark。最终验收是智元G2双臂灵巧手真机完成固定
饮料瓶分拣任务：

1. 左手抓取雪碧；
2. 左手将雪碧交给右手；
3. 右手将雪碧放入托盘左侧；
4. 左手抓取橙汁；
5. 左手将橙汁交给右手；
6. 右手将橙汁放入托盘右侧。

最终固定模型、固定工位和固定prompt执行20次完整任务，报告整体成功率、
各阶段成功率和失败类型。离线loss只用于工程检查和筛选checkpoint，不能
替代真机结果。

## 2. 不得擅自改变的冻结合同

以下内容在整个当前DM0.5项目中固定，不是“第一版暂时如此”：

### 2.1 模型输入

每个推理请求固定包含：

1. `images_head`：头部RGB；
2. `images_left_wrist`：左腕RGB；
3. `images_right_wrist`：右腕RGB；
4. 当前34D measured joint-position state；
5. 固定英文prompt。

原始图像不离线resize成PNG。OpenDM训练和推理输入管线在线执行：

```text
原始MP4/相机帧 → 保持宽高比补黑边成正方形 → resize 448×448 → VLM
```

不向DM0.5加入深度、雷达、IMU、速度、effort、wrench、头、腰或底盘状态。
这些字段只用于raw证据、离线诊断或机器人底层控制。

### 2.2 State和action

表示固定为`34D absolute joint position`：

```text
左臂7 + 右臂7 + 左手10 + 右手10
```

H5来源固定为：

```text
state/joint/position             14D
state/left_effector/position     10D
state/right_effector/position    10D

action/joint/position            14D
action/left_effector/position    10D
action/right_effector/position   10D
```

`left_effector/position`和`right_effector/position`在这份数据中是灵巧手各
10个自由度，不是机械臂末端XYZ。

H5没有关节名attrs，只提供14/10/10数组。名称与顺序由
`g2_sort/config/action_contract.json`冻结。样本间shape、dtype、数值范围和
任务运动已经兼容，但powered motion前仍必须用G2 SDK逐项确认：

- 34个索引对应的真实关节名称；
- 单位；
- 左右顺序；
- measured state语义；
- absolute command语义；
- 命令频率和有效范围。

EEF是joint action的替代表示，不是额外输入。本项目已经选择joint，不训练
EEF、relative action、velocity action或torque action版本。

### 2.3 Action chunk

- 模型输出：`50 × 34`；
- 输出语义：未来50步绝对关节位置目标；
- 训练JSONL必须显式包含`action`，不使用“未来state自动充当action”的备用
  行为；
- 推理客户端不得默认开环执行完整50步；
- powered motion时由机器人本地控制器执行插值、限位、watchdog、安全停止
  和滚动重规划。

### 2.4 Prompt

固定prompt：

```text
Use the left hand to pick up each bottle, hand it to the right hand, then place the Sprite bottle in the left tray compartment and the orange juice bottle in the right tray compartment.
```

本项目不做多prompt、语言泛化或场景泛化。

### 2.5 训练方式

- DM0.5 base + LoRA；
- 34D action projections及官方要求的time modules作为完整可训练模块；
- 数据按完整episode做`95% train / 5% validation`；
- norm stats只由train split计算；
- 正式训练先以50k step为目标；
- 100k是条件上限，不是默认必须跑满；
- 4卡和8卡都支持，不要求以8卡为单位；
- 性能比较时保持可比的global batch 32。

## 3. 源码和仓库状态

### 3.1 Git仓库

- 私有仓库：`https://github.com/BrunonXU/DM0.5.git`
- 项目分支：`main`
- OpenDM上游固定commit：
  `6ce8e7f13e0c46906b141fda1773364f18a577c2`
- OpenDM实施分支记录：`g2-dexterous-lora`

服务器clone后必须记录：

```bash
git rev-parse HEAD
git status --short --branch
git remote -v
```

不要根据本文中的历史hash猜测服务器代码版本；以包含本文的实际`HEAD`为准。
开始正式实验后，每次运行都记录项目commit及工作树diff。

### 3.2 关键项目代码

| 路径 | 作用 |
|---|---|
| `g2_sort/config/action_contract.json` | 34D名称、顺序、语义、H5来源、prompt |
| `g2_sort/training/dm05_lora.py` | G2 LoRA训练和推理配置 |
| `g2_sort/data_tools/` | ZIP索引、episode审计、转换、split和动态分析 |
| `g2_sort/deployment/shadow_inference_client.py` | 不连接执行器的shadow客户端 |
| `g2_sort/validation/validate_checkpoint.py` | checkpoint交付合同检查 |
| `g2_sort/tests/` | 34D投影和split回归测试 |
| `infra/docker/Dockerfile.opendm-g2` | 本地/服务器Docker镜像 |
| `opendm/` | 固定上游源码及必要34D补丁 |

### 3.3 相对OpenDM上游的必要修改

只保留以下核心边界：

1. `opendm/opendm/model/dm05/dm05_arch.py`
   - 官方32D action projection加载后扩展到34D；
   - 原32维权重保持，新两维标准初始化。
2. `opendm/opendm/model/dm05/dm05_lora.py`
   - 先加载官方32D base；
   - 再扩展34D；
   - 最后加载LoRA adapter。
3. `opendm/opendm/exp/dm05_exp.py`
   - 模型、数据padding和推理显式支持`action_dim=34`。
4. `opendm/opendm/dataset/g2_sort.py`
   - 注册三路图像和G2数据集。

禁止为了“跟最新版本”直接更新OpenDM `main`，也不要重新实现一套平行的
34D逻辑。

## 4. 已完成并验证的内容

### 4.1 本地环境

- 本地GPU：24GB RTX 4090D；
- Docker Engine 28.2.2；
- NVIDIA Container Toolkit 1.19.1；
- 本地镜像：`opendm:g2-34d-6ce8e7f`；
- 本地镜像使用CUDA 12.8.1、PyTorch 2.11.0+cu128；
- 默认跳过可选FlashAttention，使用eager/SDPA；
- 本地镜像ID只属于4090D主机，服务器必须自行构建并记录新image ID。

### 4.2 代码验证

- 34D投影扩展测试通过；
- 官方前32维权重保持；
- 新增两维有非零梯度；
- 95/5 episode split无交叉；
- 本地及容器回归测试：3项通过；
- checkpoint和shadow客户端合同检查代码已经存在。

### 4.3 真实数据链路

`record_20260409.zip`的一条真实正常episode已经完成：

- H5完整数值和时间戳审计；
- 三路H.265无重编码封装MP4；
- 相机`.txt`时间戳精确映射；
- 生成977行显式34D state/action JSONL；
- OpenDM loader随机读取第0、488、975样本；
- 在线补边和resize到`448×448`；
- 输出state为34D、action chunk为`1×50×34`；
- 最后两维未被截断。

另外分别从`record.tar.gz`和`record_20260413.tar.gz`抽取首个正常episode，
两条34D H5均通过有限数、连续帧号和三路时间戳映射检查；任务视频均为相同
双瓶分拣流程。

### 4.4 尚未验证

- 未下载约11.66GB DM0.5 base；
- 未运行真实DM0.5 forward/backward；
- 未运行checkpoint round-trip；
- 未运行真实LoRA step；
- 未做多GPU训练；
- 未生成全量最终manifest/split/norm stats；
- 未连接G2 SDK；
- 未运行服务器HTTP推理；
- 未做任何powered motion。

本地smoke验证不能被描述成“模型已经训练”或“DM0.5效果已验证”。

## 5. 三个原始归档

共享盘目标目录包含三个独立归档。主机名和凭据不写入Git；服务器上的实际
挂载路径由用户提供或现场发现。

| 归档 | 精确字节数 | 当前结论 |
|---|---:|---|
| `record.tar.gz` | 50,394,926,225 | 软件2.1.1、task 4；抽样joint兼容 |
| `record_20260409.zip` | 193,356,130,953 | 软件2.2.0、task 4；401个episode |
| `record_20260413.tar.gz` | 215,996,415,546 | 软件2.2.0、task 5；抽样joint兼容 |

合计：

- 459,747,472,724 bytes；
- 约459.75GB十进制；
- 约428.2GiB。

三个归档不是train/validation/test。已抽样证据支持统一进入34D joint候选
集合，最终按episode自动准入后再统一做95/5 split。

### 5.1 `record_20260409.zip`已知精确信息

- episode总数：401；
- 归档级预检查通过：367；
- 缺H5：34；
- 其中同时标记600秒异常：30；
- 30条600秒异常不是10分钟有效轨迹：
  - 实查一条只有3.8秒，机器人未开始动作；
  - 实查另一条只有16.33秒，抓雪碧失败并掉落；
  - 两条H.265均可播放，但采集未完成、H5缺失、验证字段为空。

所有缺H5episode直接排除，不尝试从视频伪造action。

### 5.2 Episode数量估算

在两个tar.gz尚未全量索引前，只能估算：

- `record.tar.gz`：约80～110；
- `record_20260409.zip`：401，已确认；
- `record_20260413.tar.gz`：约400～480；
- 合计中心估计约950。

以上只用于容量和时间规划，不能写入最终数据报告作为真实计数。

### 5.3 本地raw样本不会随Git迁移

本地4090D当前`data/raw_samples/`约3.8GB，包含：

- 三个归档各一个完整raw正常episode；
- 两个600秒异常episode；
- MP4预览；
- H5字段树和三帧真实值导出；
- `record_20260409.zip`索引副本。

该目录被`.gitignore`排除，新服务器clone后不会出现。这不是信息丢失：

- 三个大归档才是源数据；
- 服务器可从共享盘重新提取；
- Git跟踪的`data/processed/smoke/`保留一条约53MB的格式smoke；
- 不要通过Git上传460GB归档或本地3.8GB raw样本。

## 6. 当前数据工具的真实边界

已实现：

- `index_zip_archive.py`：完整ZIP中央目录和metadata索引；
- `audit_episode.py`：完整读取一个episode H5，检查shape、NaN/Inf、帧号、
  时间戳和三路视频；
- `convert_episode.py`：三路H.265无重编码封装MP4，生成显式action JSONL；
- `build_split.py`：按完整episode生成95/5 split；
- `process_split.py`：按manifest批量审计和转换；
- `analyze_dynamics.py`：逐维范围、唯一值和action→state滞后分析。

尚未实现：

- 两个tar.gz的正式全量流式索引器；
- 从tar.gz单遍提取训练必需字段并生成episode manifest的正式工具。

历史检查曾使用临时脚本抽取tar首episode，但临时脚本没有提交Git。新对话
不得声称tar全量工具已完成，也不得对压缩tar做每个episode一次的重复扫描。

服务器上需要补充一个可测试的单遍tar工具，要求：

- 顺序读取一次压缩tar；
- 对每个episode收集`meta_info.json`和必需成员存在性；
- 可选地只落盘训练必需字段；
- 不解压雷达、深度、日志、参数和额外相机；
- 支持中断恢复或至少产生明确的原子输出；
- 不使用不安全的tar路径；
- 输出与ZIP索引可归一到同一manifest schema；
- 有小样本测试后才对216GB归档执行。

## 7. 建议的服务器目录边界

项目代码与大数据必须分开。实际根路径由服务器检查后决定，不在本文假设。

建议逻辑布局：

```text
<PROJECT_ROOT>/DM0.5/               Git代码，仅小文件

<NVME_ROOT>/dm05/
├── raw_archives/                   三个原始压缩包，只读
├── staged_episodes/                仅训练必需字段
├── processed/
│   ├── videos/
│   ├── jsonl/
│   ├── audits/
│   └── manifests/
├── model_cache/                    DM0.5 base/Hugging Face cache
├── checkpoints/                    LoRA和34D密集模块
└── runs/                           配置、日志、吞吐和评估结果
```

原则：

- 原始归档复制完成后设为只读；
- 不直接从SMB训练；
- 不把大文件放进Git工作树；
- 不向共享盘写转换产物；
- 训练、推理和验证都引用同一个34D合同和norm stats；
- 服务器至少需要约1.5TB可用NVMe，推荐2TB；若不足，先报告并重新规划，
  不直接开始复制。

## 8. 新服务器第一轮：只读体检

新Codex对话第一轮只做只读检查并汇报。未经用户确认，不执行以下动作：

- 不安装或升级NVIDIA驱动；
- 不重启Docker；
- 不修改系统CUDA；
- 不开始复制约460GB数据；
- 不下载DM0.5 base；
- 不启用FlashAttention源码编译；
- 不开始训练。

### 8.1 必查项目

#### GPU

```bash
nvidia-smi -L
nvidia-smi --query-gpu=index,name,uuid,memory.total,driver_version,pstate --format=csv
nvidia-smi topo -m
```

必须确认：

- “RTX 6000D”的精确产品名；
- GPU数量；
- 单卡显存；
- 驱动版本；
- PCIe/NVLink拓扑；
- 是否存在被其他任务占用的GPU。

#### CPU、内存和磁盘

```bash
lscpu
free -h
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS,MODEL
df -hT
```

必须找到适合作为`<NVME_ROOT>`的本地高速盘，并报告：

- 文件系统；
- 总容量和可用容量；
- 是否是NVMe；
- 是否为共享网络盘；
- 是否有项目配额。

#### Docker和NVIDIA runtime

```bash
id
docker version
docker info
docker info --format '{{json .Runtimes}}'
docker images
```

只检查现状。若Docker权限、runtime或daemon异常，先报告具体错误，再决定
是否修复。

#### Git、网络和共享盘

```bash
git --version
curl --version
mount
```

检查：

- 能否访问私有GitHub仓库；
- 能否访问Hugging Face/Dexmal模型源；
- 共享盘是否已经挂载；
- 三个归档能否读到且字节数匹配；
- 共享盘到服务器的实测读取速度。

不要在聊天或Git中输出token、密码或完整凭据。

### 8.2 第一轮汇报格式

第一轮结束时必须给出：

| 项目 | 实测结果 | 是否满足 | 风险/下一步 |
|---|---|---|---|
| GPU型号/数量/显存 |  |  |  |
| GPU拓扑 |  |  |  |
| 驱动 |  |  |  |
| Docker/runtime |  |  |  |
| CPU/RAM |  |  |  |
| NVMe路径/可用空间 |  |  |  |
| GitHub访问 |  |  |  |
| 模型源访问 |  |  |  |
| SMB挂载/三个归档 |  |  |  |
| 数据读取速度 |  |  |  |

然后明确建议：

1. 是否可直接继续；
2. 是否需要系统修复；
3. 数据和模型分别放在哪里；
4. 可以并行执行哪些步骤；
5. 哪些决策仍需用户确认。

## 9. 服务器第二阶段：代码环境

只有第一轮体检通过后继续。

### 9.1 Clone与基线确认

```bash
git clone https://github.com/BrunonXU/DM0.5.git
cd DM0.5
git status --short --branch
git rev-parse HEAD
```

如果仓库已经存在，先检查remote、branch、工作树和用户改动，不使用
`git reset --hard`或覆盖未知修改。

### 9.2 构建服务器镜像

默认先使用项目精简镜像和SDPA：

```bash
docker build \
  -f infra/docker/Dockerfile.opendm-g2 \
  -t opendm:g2-34d-6ce8e7f .
```

不要把FlashAttention当成开工前置条件。只有以下条件满足后才考虑：

- 精确GPU架构已确认；
- 默认SDPA链路已经通过；
- 性能实测表明确实存在attention瓶颈；
- 编译目标只包含服务器实际架构；
- 编译时间和镜像可复现性已记录。

### 9.3 无权重回归测试

先验证：

- 容器能看到所有计划使用的GPU；
- Python/PyTorch/CUDA版本；
- `g2_sort/tests`全部通过；
- Git smoke数据可被loader读取；
- 不下载模型也能完成的数据与34D单元测试。

这一步通过不等于DM0.5 forward/backward通过。

## 10. 数据迁移和处理

### 10.1 复制顺序

体检和路径确认后，把三个归档复制到`<NVME_ROOT>/dm05/raw_archives/`：

1. `record.tar.gz`
2. `record_20260409.zip`
3. `record_20260413.tar.gz`

实际可根据共享盘吞吐并行，但避免并发过高拖垮SMB。复制必须支持断点续传，
记录源/目标字节数和耗时。不要为“确认checksum”额外多次全量读取SMB；
若源侧已有checksum直接使用，否则至少通过可恢复复制、字节数和归档读取
测试确认。

### 10.2 单遍准入，不做无目的全解码

数据准入包含：

- metadata可解析；
- duration合理；
- H5存在；
- 三路目标H.265和三路`.txt`存在；
- H5帧号从0连续；
- 34D state/action shape正确；
- state/action无NaN/Inf；
- 主时间戳连续；
- 三路相机时间戳可在`.txt`精确查到；
- H.265容器/码流可解析；
- 任务为完整双瓶流程。

不需要：

- 人工观看约950条完整视频；
- 逐帧像素解码所有视频；
- 提取雷达、深度、鱼眼、双目、日志和全部参数；
- 把三个归档预先定义成train/validation/test。

视频质量检查采用：

- 每条episode做结构、时间戳和轻量流检查；
- 按归档、日期/session和软件版本分层抽取首/中/末画面；
- 对warning或异常统计簇增加人工抽查。

### 10.3 最终数据产物

必须生成并保存：

- 归档级索引；
- 每个episode准入结果与排除原因；
- 三个来源的兼容性统计；
- 最终有效episode manifest；
- 95/5 train/validation split；
- split hash；
- 训练集norm stats；
- 三路MP4；
- 显式34D state/action JSONL；
- 数据转换配置和运行日志。

## 11. DM0.5 base和真实模型smoke

base约11.66GB，优先下载到服务器的`model_cache`，不提交Git。数据复制和base
下载在网络和磁盘不会相互严重争用时可以并行。

有base后按顺序验证：

1. 官方demo inference；
2. G2单batch真实forward；
3. G2单batchbackward；
4. 新增第33/34维梯度非零；
5. 10～100 step小样本LoRA；
6. checkpoint保存；
7. 新进程恢复checkpoint；
8. base + LoRA + 34D密集模块加载；
9. HTTP推理返回`50×34`；
10. norm stats反归一化正常。

任何一步失败时保留最小复现、完整错误和实际配置，不直接跳到多GPU训练。

## 12. RTX 6000D性能基准与正式训练

### 12.1 先跑500～1000 step

对同一冻结数据子集和global batch 32测试实际可用GPU数：

| GPU数 | 初始per-device batch | accumulation | global batch |
|---:|---:|---:|---:|
| 8 | 4 | 1 | 32 |
| 4 | 4 | 2 | 32 |

如果OOM，降低per-device batch并增加accumulation，保持global batch 32。
不得仅凭GPU型号估算训练时长。

必须记录：

- step/s；
- samples/s；
- 单卡峰值显存；
- GPU利用率；
- dataloader等待；
- CPU、RAM和磁盘吞吐；
- loss和梯度有限性；
- checkpoint保存/恢复耗时；
- world size和实际global batch。

### 12.2 决定4卡或8卡

根据实测吞吐、任务期限、GPU占用和稳定性决定。服务器训练不要求以8卡为
单位，4卡是合法配置。

### 12.3 正式训练

- 初始目标50k step；
- 每5k step保存候选checkpoint；
- 只有validation趋势和早期真机证据仍改善时才继续；
- 100k为上限；
- 训练过程中不得改变prompt、34D顺序、action表示、split或norm stats后
  仍沿用同一实验名称。

## 13. 部署边界

当前部署主线：

```text
G2采集三路RGB + 34D state
          ↓
服务器DM0.5 HTTP推理
          ↓
50×34 absolute joint action chunk
          ↓
G2本地安全控制、插值和滚动执行
```

当前不把完整模型放到Thor。Thor ARM64本地推理是远程闭环成功后的独立PoC，
不阻塞当前训练。

在G2 SDK索引、单位、频率、限位、急停和watchdog全部核对前：

- 只允许shadow mode；
- 不向执行器发送模型动作；
- 不用“训练数据能读”推断“真机映射一定正确”。

## 14. 明确禁止的偏航

新对话不得自行：

- 改用EEF；
- 加入速度、力矩、wrench、深度、雷达、头、腰或底盘；
- 改为relative action；
- 增加多prompt；
- 裁剪三路图像；
- 更新到OpenDM最新`main`；
- 下载LIBERO数据或复现LIBERO指标；
- 把460GB数据放进Git；
- 从SMB直接训练；
- 把本地raw样本不存在误判为数据丢失；
- 在没有base时声称完成真实模型训练smoke；
- 在没有G2 SDK合同和安全检查时执行powered motion。

如果认为某项冻结决策必须改变，先提供具体证据、影响范围和重新训练/部署
成本，等待用户明确批准。

## 15. 新对话启动提示词

在RTX 6000D服务器的DM0.5项目根目录打开Codex后，发送：

```text
这是DM0.5 × 智元G2双臂灵巧手项目从本地RTX 4090D迁移到RTX 6000D服务器
后的新阶段。

先完整阅读并遵循：
1. docs/07_RTX6000D_SERVER_HANDOFF.md
2. docs/02_IMPLEMENTATION_STATUS.md
3. docs/01_REPRODUCTION_PLAN.md
4. docs/03_DATA_AUDIT.md
5. docs/04_TRAINING_DEPLOYMENT.md
6. docs/05_CUSTOMIZATION_MAP.md
7. data/G2_RAW_H5_FIELD_GUIDE.md
8. g2_sort/config/action_contract.json

当前模型合同固定为：头部/左腕/右腕三路RGB + 固定prompt + 34D measured
joint-position state，输出50×34 absolute joint-position action。不得加入EEF、
速度、力矩、wrench、深度、雷达、头、腰或底盘；不得更新OpenDM基线。

第一轮只做服务器只读体检：GPU精确型号/数量/显存/拓扑、驱动、CPU、RAM、
NVMe、Docker和NVIDIA runtime、GitHub访问、模型源访问、共享盘挂载和三个
归档字节数。先用表格汇报，不安装驱动、不重启Docker、不复制460GB数据、
不下载base、不训练。发现文档与实际环境冲突时，以实际只读证据汇报，不自行
作关键决定。
```

## 16. 用户或现场环境仍需提供的信息

以下未知项不能从Git推断：

- 服务器SSH/登录方式；
- RTX 6000D精确型号和卡数；
- 服务器NVMe路径和配额；
- 共享盘在服务器上的挂载路径；
- 私有GitHub认证是否已配置；
- 模型源是否需要代理或镜像；
- 任务期限和可占用GPU时段；
- 后续G2 SDK版本、关节索引、单位和安全接口；
- 服务器到机器人网络路径；
- Thor精确型号和JetPack版本。

这些未知项不影响新对话开始只读体检，但会在对应阶段成为明确输入，不能靠
猜测补齐。
