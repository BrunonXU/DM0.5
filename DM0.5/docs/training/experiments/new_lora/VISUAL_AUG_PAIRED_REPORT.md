---
doc_type: experiment
domain: training
status: frozen
created: 2026-08-17
updated: 2026-08-17
models: [new_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/04_NEWLORA_VISUAL_AUG_PAIRED_REPORT.md]
superseded_by: []
related: []
---

# NewLora 70K/75K轻量视觉增强配对测试

更新时间：2026-08-17。

## 实验合同

- 候选：checkpoint-70000与checkpoint-75000；
- 数据：80个held-out episodes，每个episode 4个固定帧；
- 每个条件3个固定generation seeds；
- 同一state、prompt、帧和seed做paired比较；
- 条件：原图、5% crop、±2° rotation、ColorJitter、官方full augmentation p=0.5；
- 输出：10步Euler完整生成的50×34 absolute action。

这是E6-A轻量增强稳定性测试。它不移动瓶子，不包括黑图、错配episode图或时序打乱图，
因此只能回答“对训练配方内的小扰动是否稳定”，不能证明模型真正按物体位置做视觉定位。

## 结果

| 条件 | 70K physical MAE | 75K physical MAE | 70K相对原图退化 | 75K相对原图退化 | 70K动作变化MAE | 75K动作变化MAE |
|---|---:|---:|---:|---:|---:|---:|
| 原图 | 0.01077245 | 0.01079483 | — | — | — | — |
| 5% crop | 0.01078907 | 0.01081000 | +0.00001661 | +0.00001517 | 0.00147614 | 0.00148254 |
| ±2° rotation | 0.01077343 | 0.01080700 | +0.00000098 | +0.00001216 | 0.00121278 | 0.00119545 |
| ColorJitter | 0.01077070 | 0.01078713 | -0.00000175 | -0.00000770 | 0.00070121 | 0.00070167 |
| 官方full p=0.5 | 0.01076356 | 0.01077738 | -0.00000889 | -0.00001745 | 0.00130737 | 0.00131461 |

70K与75K增强退化差的episode配对95%置信区间：

- crop：[-0.00002419,+0.00002078]；
- rotation：[-0.00000887,+0.00003128]；
- ColorJitter：[-0.00002003,+0.00000762]；
- 官方full：[-0.00002725,+0.00000977]。

全部跨0，两个checkpoint在这些轻量扰动下无法统计区分。原图上70K只比75K低
0.00002238，差异也很小。

生成seed波动为70K 0.0056380、75K 0.0054703。75K的增强动作变化仅为seed波动的：

- crop：0.271倍；
- rotation：0.219倍；
- ColorJitter：0.128倍；
- 官方full：0.240倍。

## 结论

1. 两个checkpoint都对配方内的轻微crop/rotation/color jitter较稳定；
2. 75K没有表现出优于70K的视觉增强鲁棒性；
3. 增强引起的动作变化小于扩散seed噪声；
4. “稳定”也可能意味着视觉不敏感，不能据此证明视觉grounding；
5. 真机黄色纸盒、瓶位和遮挡问题仍需E6-B：固定真实场景下做瓶位移动、黑图、错配图和
   时序打乱图的shadow反事实，禁止下发机器人。

结果源：server/evaluations/NewLora_validation_v1/visual_aug_paired_summary.json。
