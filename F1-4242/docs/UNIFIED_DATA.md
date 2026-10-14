# F1 统一数采与本地存储

2026-10-12。部署进度和实机验收单独见 [STATUS](STATUS.md)。

## 同一训练合同，保留来源证据

三路 RGB 原图、左臂7轴＋右臂7轴＋左右夹爪。手臂使用硬件绝对角度（度），夹爪0开100闭。state 是真实反馈，action 是实际发布目标，不用反馈回填动作。头/身体上下文和原始时间戳作为来源信息保留，不混入16D策略动作。没有采集深度图。

默认30Hz；新 Pico 以真实回调身份和本机接收时刻配对，原始相机 header 另存，不能把本机接收配对称为曝光同步。相机尺寸据实保存，当前目标头部1280×720、双腕640×480；网页预览不决定数采频率，数据入口不更改相机驱动配置。

F710沿用已验证的异步JPEG pack/JSONL原件和边界记录，避免改动控制链；新独立Pico使用统一的可恢复 JPEG pack。两者通过来源适配器进入**同一个固定版本官方 LeRobot v3 writer**。原始审计附件允许按来源不同，正式训练字段相同；不把历史格式直接改名为v3。

旧LXL MCAP不含最终手臂命令，旧meaxiao27D有动作回填及身体/底盘语义问题：`inspect` 输出缺项及阻塞原因，不能自动获得训练资格。它们的原始记录和已有回放继续可读。旧LXL加密录制程序仍是兼容入口；本次没有改写其控制和录制内部代码。新统一Pico录制入口对应已验证来源合同的独立上肢Pico，不允许冒用其合同采旧LXL命令。

## 机器人本地路径与停止

```text
/home/user/Bruno/data/
  pico_data/                    # 新独立 Pico 原始记录；.staging / recovery / discarded 分开
  hil_data/demonstration/        # F710 人工示教原件
  hil_data/policy_hil/           # 完整策略 HIL 原件
  datasets/                     # 官方 v3 派生候选/审核后导出
```

新Pico：4242准备独立录制服务 → 开始录制 → 结束并审核 → 保存这条录制。录制时不编码三路MP4；保存原始片段后，DATA文件夹中可进行原图检查。保存原始片段≠训练审核通过。

F710：原三端启动和START接管语义保持。结束使用网页“结束并保存”或现场运行终端Ctrl+C；正常提交后才入转换队列。自由接入不保存训练图片。旧runtime/raw、lxl/dagger_data仍索引；LXL旧MCAP原路径不移动。

配置只在后端：runtime/storage.json来自configs/storage.example.json；runtime/recording.json来自configs/recording.unified.example.json。HIL包装器`--data-root`和runtime/hil.json读取根必须一致。原始存储与程序runtime分开；配置修改不改变已经启动的会话。

## 离线转换与审核

在安装固定LeRobot依赖的处理机执行，工作目录为F1-infra根；源数据可位于该机器的显式本地/挂载目录。不会自动下载或上传：

```bash
python -m recording.unified inspect --source /path/to/source
python -m recording.unified review-template --source /path/to/f710-run \
  --dagger-root /path/to/DAgger --output /path/to/review.json
python -m recording.unified export --source /path/to/f710-run \
  --dagger-root /path/to/DAgger --lerobot-root /path/to/locked/lerobot \
  --output /path/to/data/datasets/candidate
```

Pico源由capture.json自动识别，不需要dagger-root。默认30fps，可显式选择20fps重新采样。Home、保护保持、交接、来源epoch/相机不连续形成边界。Pico原有选择逻辑保持；F710从DAgger依赖读取选择实现，实际部署版本见STATUS。

2026-10-13开发版F710采用固定时间网格的因果采样：图像和state取网格时刻已经可用的最新有效观测，action独立取该时刻之前最后实际发布的有效命令。必要时短暂沿用一组观测，`source_observation_monotonic_ns`、`observation_age_ns`、`observation_reused`、`command_monotonic_ns`和采样版本随`f1_source.jsonl`保留；沿用不是新曝光或新增独立示教。不插值图像、不把缺格压缩成连续时间，也不外推到最后有效观测之后。原有边界、观测间隔及相机/反馈/命令时效要求保留，且在目标网格时刻重新验证。

候选导出不需要伪造人工审核。审核模板绑定源manifest SHA、片段ID、任务、审核人；新版还记录`selection_sha256`，绑定实际分段、采样时间和动作来源。新版F710禁止用旧片段编号审核文件直接批准重新分段的数据，须重建模板并审核。人工填写accepted_segments及decision后，另存新目录并传`--review /path/to/review.json`。每次导出均逐帧用官方reader核对state/action、episode、时间轴和三路解码；写入失败保留.partial，禁止覆盖重试或自动删除。

依赖固定在recording/unified/lerobot_binding.json；不同模型在训练适配器里做resize、归一化、维度/表示转换。v3格式兼容不代表任意模型输入无需适配。

训练发布可用 `python -m recording.unified.release --spec /path/to/spec.json --output /path/to/release.json`。spec为`{"datasets":[{"path":"/path/to/reviewed-v3","split":"train"}]}`；仅接受审核、回读与哈希通过的数据。同一原始会话不得跨train/validation/test，同一来源观测不得出现在多个导出数据集中；单个固定网格导出中明确记录的短暂观测沿用允许保留。统计仅从train计算，原件和已发布数据不改写。

## 异步与上传边界

`python -m recording.unified.jobs scan --runtime /path/to/runtime`只建立幂等持久转换任务；Pico保存后后台定期扫描，HIL可由同一只读扫描入口触发。`run-one`要求显式依赖目录，检测数采/遥操进程时暂停启动；处理中发现新采集则停止转换、保留.partial并标记失败待处理。它不发送机器人命令，不是服务器上传器。

**归档上传默认关闭，未启动新的服务器归档任务。**配置保留静息、限速、单任务、不自动删除本地的策略；启用还需单独配置并验证传输worker。既有服务器回放编码流程独立，已有历史F710原始帧缓存不能说成从未上传。新格式原图可以在本机检查，统一Pico视频派生需离线处理；本轮不伪装为已回传自动视频。

## 验收边界

软件：恢复/完整性、来源与边界、目录兼容、队列、官方reader和HTTP回放分别测试。真实F710旧记录已用于转换验证，仍待人工片段审核。新Pico需要现场短录制，证明实际30Hz、真实命令来源、结束保存和DATA回看；离线假数据和ARM导入不能代替这个验收。原控制保护和G2不改动。
