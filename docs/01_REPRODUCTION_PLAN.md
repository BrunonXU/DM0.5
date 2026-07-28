# DM0.5 × 智元 G2 双臂灵巧手饮料瓶分拣复现计划

## 一、目标与已确定方案

本项目不复现 LIBERO 指标。最终目标是在固定工位上验证 DM0.5 经 G2 数据 LoRA 适配后的真实机器人效果：

> 左手依次抓取雪碧和橙汁，交接给右手，右手分别放入托盘对应区域。

已锁定以下决策：

- 本地 RTX 4090D 负责环境开发、数据验证、短程训练 smoke 和单卡推理，不承担正式质量训练。
- 正式训练使用服务器4卡或8卡；型号和数量后续根据资源、任务紧急程度及1000-step实测决定。
- 第一版采用三路 RGB：头部、左腕、右腕；遵循官方补边后 `448×448`，不主动裁边。
- 动作空间采用 `34D absolute joint-position`：
  - 左臂7 + 右臂7 + 左手10 + 右手10。
  - `state` 使用同顺序的实测反馈位置。
  - `action` 使用采集时记录的绝对位置目标。
  - H5提供固定的14D双臂、左手10D和右手10D数组，但不包含关节名attrs；
    名称与顺序由`g2_sort/config/action_contract.json`冻结，powered motion
    前必须再与G2 SDK逐项核对。
  - EEF数据只保留作诊断，不进入本项目模型。
- 模型采用 LoRA，并完整训练扩展后的34D输入/输出动作投影层。
- 数据只做 `95% train / 5% validation` 两分，最终测试是真机。
- 第一版使用一条固定英文 prompt，不做语言或场景泛化：
  - `Use the left hand to pick up each bottle, hand it to the right hand, then place the Sprite bottle in the left tray compartment and the orange juice bottle in the right tray compartment.`
- 部署主线是模型保留在服务器，G2远程请求推理；Thor本机推理作为后续可选移植，不阻塞第一版。
- 正式真机效果以固定条件20次完整任务的成功率和失败阶段分布进行报告，不预设人为通过门槛。

## 二、核心接口与官方边界

### 数据与模型接口

- OpenDM JSONL 每帧包含三路图像、固定 prompt、34D state 和34D action。
- 训练采用显式 action，不使用“未来 state 自动充当 action”的备用行为。
- DM0.5 本身不限定 joint 或 EEF；第一版选择 joint 是因为它最贴近现有采集命令，并避免额外引入坐标系、姿态表示和双臂 IK。
- OpenDM 当前默认 `action_dim=32` 且会截断超出的维度，因此必须把模型动作头和数据 padding 统一扩展到34D。
- 推理服务输入为三路图像、当前34D state和固定 prompt，输出形状为 `chunk_size × 34` 的绝对关节目标。

### 官方能力与项目自定义

官方 OpenDM 提供：

- x86 CUDA Docker环境；
- JSONL数据注册、图像预处理和归一化；
- LoRA及多卡训练；
- checkpoint保存恢复；
- 单GPU HTTP推理服务。

本项目需要补充：

- G2原始归档转换与质量检查；
- 34D动作头扩展；
- G2动作契约和远程推理客户端；
- 动作chunk调度、限幅、超时和安全停止；
- 如后续需要，Thor ARM64环境移植。

计划基于当前已核对的 [OpenDM commit `6ce8e7f`](https://github.com/dexmal/opendm/tree/6ce8e7f13e0c46906b141fda1773364f18a577c2)，实施时保持该版本，不直接跟随变化中的 `main`。

## 三、流程分类

### 阶段A：术前准备与官方链路确认

- 在项目根目录建立独立项目，主计划保存在
  `docs/01_REPRODUCTION_PLAN.md`。
- 修复本地 Docker 权限并确认 NVIDIA Container Toolkit，验证容器可访问4090D。
- 拉取并固定 OpenDM 源码和容器版本；此阶段暂不下载大模型。
- 到官方推理/smoke阶段再下载约11.66GB的 DM0.5 base。
- 先运行官方 demo inference 和极短 LoRA smoke，确认环境、训练、保存、恢复和HTTP推理链路成立；不复现 LIBERO benchmark。

### 阶段B：数据整理与训练合同冻结

- 将三个大归档复制到训练服务器本地 NVMe 后处理，避免直接从共享盘持续训练。
- 完整索引元数据和H5，但不无目的地解码全部视频。
- 排除600秒异常录制、缺少H5、损坏流、时间戳异常和关键字段缺失的数据。
- 三个归档不天然对应 train/validation/test：
  - 对 action schema、数值范围、关节顺序和任务流程兼容的有效episode合并。
  - 旧AID或旧软件版本若控制语义不兼容，第一版直接排除。
- 按完整episode分层做95/5划分，禁止将同一episode的相邻帧分到两侧。
- 原始视频优先无重编码封装为可随机定位的MP4，并校验视频帧、H5帧和时间戳一致性。
- 生成34D动作契约清单，按采集接口约定解释H5的14/10/10数组；H5本身没有
  关节名attrs。单位在训练中保持原始一致，真机接入前必须与G2 SDK核对名称、
  索引、单位和命令语义。

### 阶段C：G2数据格式与34D模型适配

- 将三路RGB、34D反馈state、34D绝对action和固定prompt转换为官方OpenDM JSONL。
- 使用训练集计算独立 norm stats，并随checkpoint交付。
- 将DM0.5动作空间从32扩到34：
  - 继承原32维投影权重；
  - 新增两维采用标准初始化；
  - LoRA训练模型主体；
  - 34D动作投影及官方要求的时间模块作为完整可训练模块保存。
- 验证模型没有再截断最后两维，并确保新增维度具有梯度和非恒零输出。
- 训练、4090D推理、服务器推理和后续Thor移植始终使用同一34D配置。

### 阶段D：本地验证与服务器正式训练

- 4090D完成：
  - 单batch前向/反向；
  - 小样本过拟合和短程LoRA smoke；
  - checkpoint round-trip；
  - 离线轨迹预测；
  - HTTP服务与模拟G2客户端。
- smoke只验证工程链路，不评价最终模型能力。
- 服务器候选确定后，用同一数据子集和配置各跑500～1000稳定step，比较吞吐、显存、GPU利用率和数据等待。
- 保持可比的 global batch 32：
  - 8卡通常采用单卡batch4、累积1；
  - 4卡通常采用单卡batch4、累积2；
  - 若三路图像导致显存不足，降低单卡batch并增加累积。
- 正式LoRA先运行约50k步并保留周期性checkpoint；只有验证趋势和真机早期结果仍持续改善时，才继续到100k上限。
- 离线validation只用于发现转换错误、过拟合和筛选候选checkpoint，不取代真机效果。

### 阶段E：服务器远程推理与G2接入

- checkpoint和推理环境保留在服务器；机器人端不保存完整模型。
- 服务器部署包包含 base、LoRA、34D密集模块、模型配置、norm stats、固定源码commit和数据manifest。
- G2客户端采集三路同步图像和当前34D反馈状态，调用服务器并接收34D action chunk。
- 真机SDK索引映射放在本阶段完成，但必须严格对应训练时保存的34D顺序。
- 模型服务不承担硬实时控制：
  - G2本地控制器负责插值、限位、watchdog和安全停止；
  - 采用滚动重规划，不默认开环执行完整50步；
  - 实际执行前缀根据端到端P95延迟和30Hz数据频率确定；
  - 若网络延迟、抖动或断连不满足安全条件，则停止真机动作，而不是继续执行旧chunk。
- Thor本机推理在远程闭环完成后再做独立PoC；当前OpenDM没有官方Thor/ARM64部署保证。

### 阶段F：分级真机验证与交付

- 依次完成：
  1. shadow mode，仅记录模型输出；
  2. 低速无物体运动；
  3. 单瓶抓取；
  4. 左右手交接；
  5. 单瓶放置；
  6. 两瓶完整流程。
- Powered motion开始前必须确认急停、低速比例、关节/速度限制、超时safe hold/stop和现场看护。
- 对候选checkpoint先做少量低速试验，选出表现最好的版本。
- 最终固定模型、固定工位和固定prompt运行20次，记录：
  - 完整任务成功率；
  - 雪碧抓取/交接/放置；
  - 橙汁抓取/交接/放置；
  - 网络、模型、控制或机械失败类别；
  - 三路视频、state、原始action和安全过滤后action。
- 第一版结束后依据20次结果决定是否值得继续补数据、增加泛化或迁移Thor，不在本轮提前扩展范围。

## 四、验收与交付物

### 阶段验收

- 环境：官方demo、短程LoRA和单GPU推理均可重复运行。
- 数据：所有训练episode有效，34D顺序一致，无NaN/Inf，视频和H5严格对齐。
- 模型：34D前向、反向、保存、恢复、LoRA加载和HTTP推理全部通过，最后两维未被截断。
- 训练：4/8卡配置具有记录完整的global batch、吞吐和实际运行配置。
- 部署：服务器能够稳定接收三路图像和34D state并返回34D action chunk。
- 真机：完成20次固定任务评估并生成可审计结果，而不只保留演示视频。

### 最终交付

- 固定源码commit和容器环境；
- 数据审计结果、兼容性结论和95/5 split manifest；
- 34D动作契约与固定prompt；
- base引用、LoRA、34D密集模块、norm stats和checkpoint清单；
- 服务器推理与G2客户端部署说明；
- 20次真机验证报告、日志和失败分类；
- 项目主计划文档 `docs/01_REPRODUCTION_PLAN.md`。

## 五、延后但有明确处理方式的未知项

- H20/RTX 6000D准确型号、卡数和拓扑：到服务器确定时体检，不影响前期开发。
- G2 SDK单位、命令频率和安全接口：不阻塞训练，但必须在真机接入前核对。
- Thor具体型号和JetPack版本：不属于第一版远程推理验收条件。
- 服务器网络位置：远程真机前必须证明位于可信稳定网络并通过延迟测试；不满足则改用本地4090D服务或暂停真机闭环。
- 不在第一版同时训练relative action、EEF action、多prompt或场景泛化版本，避免失去主线。

主要官方参考：

- [DM0.5模型与运行要求](https://huggingface.co/Dexmal/DM05)
- [DM0.5自定义数据SFT指南](https://github.com/dexmal/opendm/blob/6ce8e7f13e0c46906b141fda1773364f18a577c2/docs/en/dm05_finetuning.md)
- [DM0.5 LoRA参考配置](https://github.com/dexmal/opendm/blob/6ce8e7f13e0c46906b141fda1773364f18a577c2/docs/en/dm05_libero_lora_training.md)
- [OpenDM action变换定义](https://github.com/dexmal/opendm/blob/6ce8e7f13e0c46906b141fda1773364f18a577c2/opendm/data/transforms.py)
