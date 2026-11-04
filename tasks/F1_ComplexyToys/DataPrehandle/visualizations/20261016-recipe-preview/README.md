# V1真实三路图：ROI、224输入与正式增强预览

日期：2026-10-16（北京时间）。使用「V1复位修复后」原件，通过当前共享`process_image()`和真实DM0.5增强helper生成。不是AI生成图，不改变原始数据；只做离线预览，没有接入新的训练或推理代码。

## 直接看图

| 观测 | 四阶段对照 |
|---|---|
| 约5.2秒，接近木块 | [原图→ROI→当前224输入→候选增强](full-effects/overview-row-0150.png) |
| 约15.2秒，右夹爪已夹住木块 | [推荐主图](full-effects/overview-row-0450.png) |
| 约22.3秒，容器上方 | [后段对照](full-effects/overview-row-0660.png) |
| 同一15.2秒观测的三个随机seed | [增强变化对照](full-effects/augmentation-seeds-row-0450.png) |

每张总览按行排列主图、左腕、右腕。第1列是原图（主图红框仅为展示标注，不进入模型）；第2列是固定ROI后的原尺寸像素，两腕不裁；第3列为当前共享处理器的真实224×224像素；第4列为本轮正式增强策略的候选训练输入。像素随后按`x/127.5−1`进入模型，PNG显示归一化前的相同像素，不做展示auto-contrast。

第3列对应已实现的确定性路径，后续推理也使用它。第4列尚未接入生产训练，不能称作已运行新模型的真实batch；下一实施阶段需从实际dataloader再导出对照核验。

## 处理与覆盖

- 主图1280×720裁`[307,28,1229,720)`；腕图640×480保持全图；等比缩放、黑色补边到224。
- 在确定性224之后，主图95%面积正方形随机裁切、±2°旋转、ColorJitter分别p=0.5；腕图只做ColorJitter。brightness0.3、contrast0.4、saturation0.3、hue0。
- 直接复用DM0.5 `TrainingTransformPipeline` 的真实增强实例，裁剪输出由448改224；不重复调用其Pad/Resize。每相机每操作独立派生seed，保留完整Albumentations replay参数，包含插值/边界填充实际行为。
- `full-effects/`使用20261020、20261019、20261016三种seed，展示主图全部操作、裁切＋颜色、仅旋转等实际随机组合；不是强制每次全部增强。根目录还保留最初三个相邻seed的输出，不覆盖历史预览。
- 本次所展示时刻中物体、夹爪和孔位没有被候选主图几何增强裁掉；这是抽样目视结论，不是整条轨迹或未来场景的全覆盖保证。
- 真实DM helper在补黑边之后做颜色增强，因此contrast可能使黑边变灰；某些seed会使白桌更亮、对比度降低。这不是RGB通道故障。当前预览忠实保留参数，若收窄颜色强度应另建recipe/预览版本，不悄悄改变本组图片。插孔依赖细节，需要在真实训练batch继续检查边缘/孔位可见性。

## 像素、来源与复现

推荐组的 [manifest.json](full-effects/manifest.json) 保存源episode、训练row、observation/frame、源JPEG SHA、ROI、处理器/helper/旧参考recipe/预览脚本SHA、依赖版本、seed和每次增强的replay参数。

该组58个PNG：4张总览与54张未经展示放大的原生尺寸图片，逐张在manifest登记；原生图片位于 [pixels](full-effects/pixels/)。8个源文件哈希未变；27种增强均通过同seed重跑和replay逐像素一致检查。当前处理器重复输出一致，源payload SHA匹配原件。

生成脚本：[preview_recipe_inputs.py](../../scripts/preview_recipe_inputs.py)。使用6000D已有`opendm:g2-34d-fa2` CPU环境，不联网、不安装依赖、不占GPU；实际版本Python3.10.20/Pillow12.2.0/NumPy1.26.4/Albumentations2.0.8/OpenCV4.11.0。总览使用Pillow排版，独立224像素图保留精确数组。

在**6000D Bash**复现到新目录；输出及检查记录写入该目录的`manifest.json`，终端输出为本次执行摘要。以下与本次实际命令相同，仅将输出从已有`full-effects`换为`reproduction-01`；脚本拒绝覆盖已有目录。UID/GID为这台6000D当前账号，换机时需按账号核对。

```bash
cd /home/user/projects
sg docker -c 'docker run --rm --network none \
  --user 1006:1008 \
  -e USER=user \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e NO_ALBUMENTATIONS_UPDATE=1 \
  -v /home/user/projects:/home/user/projects:ro \
  -v /home/user/projects/tasks/F1_ComplexyToys/DataPrehandle/visualizations:/home/user/projects/tasks/F1_ComplexyToys/DataPrehandle/visualizations:rw \
  -v /usr/share/fonts:/usr/share/fonts:ro \
  --entrypoint /opt/conda/envs/opendm/bin/python \
  opendm:g2-34d-fa2 \
  /home/user/projects/tasks/F1_ComplexyToys/DataPrehandle/scripts/preview_recipe_inputs.py \
  --output /home/user/projects/tasks/F1_ComplexyToys/DataPrehandle/visualizations/20261016-recipe-preview/reproduction-01 \
  --rows 150 450 660 \
  --seeds 20261020 20261019 20261016'
```

必须显式指定上述三个seed才对应推荐组；脚本默认seed对应最初一组预览。源数据路径、字体、helper和处理器需要存在；此预览脚本不安装或改动依赖。

正式增强由训练adapter统一拥有，core内部增强关闭；推理不随机增强。最新完整决定见 [训练recipe](../../../TRAINING_RECIPE.md)、[推理recipe](../../../INFERENCE_RECIPE.md)，实现状态见 [STATUS](../../../STATUS.md)。
