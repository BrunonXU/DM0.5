---
created: 2026-09-16
type: implementation
status: reader_engineering_validation
---

# F1 state19 / EEF23 reader

这是[正式recipe](../../../docs/training/dm05_eef_delta_recipe.md)的任务侧数据适配。共享DM0.5网络、flow matching和既有G2代码均未修改；当前状态见[training_plan](../../../docs/training_plan.md)。

## 已实现的合同

- 原包保持state21/action19；输入取反馈前16维＋已复核重建速度当前3维，body5不进入模型。
- H50从`t`开始；双臂以当前反馈FK为共同时间锚点，每臂独立计算`inv(T_feedback[t]) @ T_mapped_command[t+k]`，位置m、旋转矩阵前两行6D；夹爪2＋底盘3得到23D，补齐32D。
- FK缓存绑定source SHA、cache SHA、时间轴、左右顺序、单位、执行映射与运动学合同。目标使用已绑定的映射后FK，reader不二次裁剪关节；跨合同数据不能混用统计或Dataset。
- 继承原始bool masks、split及H50起点限制；缺失当前底盘估计不能填0；本版只消费完整有效窗口，不复制末动作或跨unknown/复位间隔。
- state双臂按用户09-17确认使用train-only有效min/max；底盘保留q01/q99及退化时min/max，状态夹爪固定量程。action位置/速度按有效H50元素MeanStd、旋转identity、夹爪固定量程、常量scale1、padding无监督。统计保存各字段range_policy，旧工程统计不自动更新。
- 按源录制均匀、再按有效起点均匀采样；`EpisodeBalancedSampler.set_epoch()`与rank/world_size支持可复现DDP分片。正式trainer须使用此sampler，不能又叠加默认DistributedSampler。
- 图像取原MCAP中与既有`camera_source_indices`完全相同的压缩消息；逐图SHA、索引、header/log时间保留，核对没有未来帧且log age与来源一致。不能用2Hz复核缩略图替代。
- 三图复用共享黑色补边/448预处理；训练头图复用完整G2温和增强，腕图仅ColorJitter；验证不随机增强。`ChatTokenization`及`TrainingCollator`保持共享原实现，输出action/mask形状`[B,50,32]`。

`allow_candidates=True`仅允许工程检查现有候选；不会修改来源批准状态或把数据升级为正式训练集。`FK valid`只表示缓存中的FK数值有效，不代表IK轨迹或机器人执行通过。

## 数值与原图审计

在6000D、`/home/user/projects`执行；选择新的输出目录。已有结果不要覆盖。

```bash
PYTHONPATH=F1/foam_sort_whole_body/models/DM0.5 \
/tmp/f1-foam-audit-venv/bin/python -m f1_data.audit \
  --cases F1/foam_sort_whole_body/results/base_relabel_v3_autonomous/cases \
  --pose-cache F1/foam_sort_whole_body/results/kinematics_validation_20260916/pose_cache \
  --output /tmp/f1-reader-audit-new \
  --allow-candidates \
  --raw-root /mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw \
  --images-per-episode 3
```

`--images-per-episode 3`只生成每条首/中/末三组观测的原图cache，适合smoke；`-1`为所有有效起点提取三图。部分cache遇到未缓存起点直接报错，不偷偷换图或填黑图。正式训练前须完成全有效起点图像缓存与全读验收。

全图提取后，使用`python -m f1_data.image_readback --help`进行独立读回：绑定原相机索引SHA，复算最新因果**有效**帧，再通过F1Dataset全读原始RGB及数值。8条现有候选的完整命令与结果见[全图审计](../../../results/reader_full_images_20260916/README.md)。该检查不加载模型，不包含全窗口增强/tokenization或真机验收。

## 接入共享模型数据接口

在已有DM0.5环境中，把本目录父目录与`DM0.5/opendm`加入`PYTHONPATH`。reader本身不强制依赖torch，图像增强和tokenization在已验证模型环境中使用。

```python
import json
from f1_data import Episode, F1Dataset, Normalizer
from f1_data.images import ImageLoader
from f1_data.reader import EpisodeBalancedSampler
from opendm.data.transforms import ChatTokenization
from opendm.data.collator import TrainingCollator

episodes = [Episode(source_npz, pose_npz, allow_candidates=False)]
normalizer = Normalizer(json.load(open(stats_path)))
dataset = F1Dataset(
    episodes, normalizer, ImageLoader(images_root, training=True),
    ChatTokenization(processor, image_keys=["images_1", "images_2", "images_3"]),
)
sampler = EpisodeBalancedSampler(dataset, count=len(dataset), seed=42)
collator = TrainingCollator(processor.tokenizer.pad_token_id)
# Pass dataset/sampler/collator to the task training DataLoader.
```

这里的state/action已完成合同转换、分字段归一化和补齐；**不要再运行共享BuildAction、ActionRelative、Normalize或PadAction**。夹爪/底盘不是EEF增量；state19与旧joint action19长度相同也不能互相替代。

CPU真实processor复核入口为`python -m f1_data.model_smoke --help`，只加载本地processor，禁用CUDA/网络，不加载模型权重或训练。可使用既有G2 checkpoint内的通用processor文件，不能使用其中G2模型adapter或动作stats。

## 验证范围与剩余工作

本次记录见[reader工程验证](../../../results/reader_validation_20260916/README.md)。单测覆盖非平凡旋转锚点、未知速度跨界、来源哈希、train/val隔离、常量与稀疏维、均匀源采样、DDP分片及共享tensor接口；原数据只读。

2026-09-17已新增[任务Exp/Trainer/DataLoader接线](../f1_training/README.md)，`experiment.yaml`迁移为既定19D/23D配方；8条全图缓存已有读回证据。正式数据统计、完整模型前向/反向及正式验证协议仍须收口，当前状态见[当前方案](../../../docs/training_plan.md)。

当前小批stats仅用于工程验证。审计真实发现验证条目部分关节超出6条训练来源的q01/q99范围；不得把这份stats发布部署，也不把validation加入fit补范围。随正式train范围重算、复核裁剪率及反量化误差。动作MeanStd无标签裁剪。

## 部分底盘监督合同（2026-09-17）

v2保留原有效上肢窗口，输出仅按维mask未知底盘；当前未知底盘state19最后3槽通过`f1_data.state_tokens.make_tokenizer`编码为unknown。旧直接ChatTokenization示例仅适用于底盘全已知的v1。正式训练从`f1_training.data.Release(...).dataset(...)`读取已绑定发布包，入口已使用新tokenizer；不要绕过state_mask。统计采用等价分批累计，仅有效train值参与；生成评估也按有效标签计算，未知不计为停车或零误差。范围和状态见[当前方案](../../../docs/training_plan.md)。
