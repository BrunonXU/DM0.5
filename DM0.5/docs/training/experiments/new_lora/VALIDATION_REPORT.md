---
doc_type: experiment
domain: inference
status: frozen
created: 2026-08-17
updated: 2026-08-17
models: [new_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/02_NEWLORA_VALIDATION_REPORT.md]
superseded_by: []
related: [CHECKPOINT_VALIDATION_DESIGN.md]
---

# NewLora Validation v1 实验报告

## 1. 报告状态与当前结论

更新日期：2026-08-17。

本报告记录 NewLora 已经真实执行的验证实验、冻结输入、随机 seed、全部指标、结果、
统计方法、产物路径，以及每项实验能支持和不能支持的结论。未执行实验不会被写成已完成。

当前离线结论：

- 5K至75K共15个checkpoint已全部完成held-out FM和轻量完整动作生成；
- 65K、70K、75K已完成密集Pass B；
- 65K在密集验证中弱于70K和75K，退出最终双候选；
- 70K与75K总physical MAE在95%配对置信区间下无法区分；
- 75K在双臂误差、训练分位范围外输出、seed稳定性和轨迹动态指标上更好；
- 70K在左手误差和首动作跳变上更好；
- 当前离线推荐为`75K Primary / 70K Fallback`；
- 仍需视觉反事实、历史失败回放和真机闭环，不能表述为最终真机赢家。

## 2. 冻结实验合同

### 2.1 数据与动作

- 训练集：800 episodes；验证集：80个训练从未见过的episodes；
- 验证集可用观测：90,244；验证图像：`NoAugmentationPipeline`；
- normalization：只使用NewLora 800条训练集计算；
- 相机顺序：head、left wrist、right wrist；
- 输出：`50 x 34`；前14维手臂relative，后20维灵巧手absolute；
- state description：14个`JOINT`加20个`GRIPPER`；
- 完整生成：10步Euler；
- 所有checkpoint使用相同episode、帧索引和seed schedule。

### 2.2 冻结文件身份

| 产物 | SHA256 |
|---|---|
| FM/Pass B manifest：80×16 | `5a5b35924791dce76be8646e280ae5f12796872cece9ee23c11cc038e6adaf9a` |
| Pass A action manifest：80×4 | `f8cc3f3f3847c8d95ef1adff18bb660b58c38b661c7cc95ea0fea5f7286ea7a8` |
| NewLora train-only norm stats | `6ae5b97f9deb9fdb7c2567f635837c67a6a96c56b8bcf2824d158eac0c470a26` |

## 3. 已执行实验总览

| 编号 | 实验 | 候选 | 输入规模 | Seeds | 状态 | 目的 |
|---|---|---:|---:|---:|---|---|
| E0 | mixed-action单元测试 | 无 | 合成34D动作 | 无 | 4/4通过 | 验证14D relative+20D absolute |
| E1 | 单episode GPU smoke | 5K | FM 16帧；动作4帧 | 各1 | 完成 | 验证真实输入输出合同 |
| E2 | Held-out FM Pass A | 15个 | 80×16 | 3 Flow seeds | 完成 | 局部Flow泛化诊断 |
| E3 | 完整动作生成 Pass A | 15个 | 80×4 | 2 generation seeds | 完成 | 全候选部署路径筛查 |
| E4 | 完整动作生成 Pass B | 65K/70K/75K | 80×16 | 5 generation seeds | 完成 | 密集比较近邻候选 |
| E5 | Episode配对bootstrap | 65K/70K/75K | 80个episode差值 | 20,000次 | 完成 | CI与episode win rate |
| E6-A | 轻量视觉增强配对 | 70K/75K | 80×4 | 3 generation seeds | 完成 | crop/rotate/color稳定性 |
| E6-B | 强视觉反事实 | 35K/70K/75K | 待冻结 | 待冻结 | 未执行 | 瓶位、黑图、错配与时序打乱 |
| E7 | 历史失败回放 | 70K/75K | 待整理 | 多seed | 未执行 | 抓空后的新请求行为 |
| E8 | 真机配对A/B | 70K/75K | 待冻结场景矩阵 | 重复trial | 未执行 | 最终闭环成功与安全 |

## 4. E0与E1：合同测试和GPU Smoke

E0验证：

- state_desc严格为14个手臂`JOINT`加20个手指`GRIPPER`；
- ActionRelative只对前14维做`action-state`；
- 后20维灵巧手保持absolute；
- ActionAbsolute可无误差还原；
- validation整段mixed-to-absolute不会把50步delta逐步累加；
- 4项单元测试全部通过。

E1使用5K checkpoint、一个真实验证episode执行FM和10步Euler完整生成：

| 指标 | 数值 |
|---|---:|
| FM loss | 0.035330 |
| Physical action MAE | 0.032203 |
| First-action jump mean | 0.083627 |

Smoke只证明链路、形状和语义正确，不进入checkpoint排名。

## 5. E2：15个Checkpoint Held-out FM

协议：`80 episodes × 16固定位置 × 3固定Flow seeds = 3,840次暴露/checkpoint`。
先在episode内聚合，再对80个episode做macro aggregation。

| CKPT | FM loss ↓ | CKPT | FM loss ↓ | CKPT | FM loss ↓ |
|---:|---:|---:|---:|---:|---:|
| 5K | 0.03244735 | 30K | 0.01500583 | 55K | 0.01519144 |
| 10K | 0.02091901 | 35K | **0.01390156** | 60K | 0.01604102 |
| 15K | 0.01843215 | 40K | 0.01397494 | 65K | 0.01659598 |
| 20K | 0.01696888 | 45K | 0.01396935 | 70K | 0.01685281 |
| 25K | 0.01700137 | 50K | 0.01455037 | 75K | 0.01719385 |

FM在35K附近最优，之后逐渐恶化。这只能表述为held-out局部Flow目标退化，不能直接
称为整体策略过拟合，因为完整生成仍改善。FM输出包含macro mean/median/P90、每
episode mean/median/max和每batch/seed记录。模型forward只返回聚合loss，因此本轮
没有FM关节组loss。

## 6. E3：15个Checkpoint完整动作生成 Pass A

协议：`80 episodes × 4固定位置 × 2 generation seeds = 640次完整生成/checkpoint`。

| CKPT | Physical MAE ↓ | Norm MAE ↓ | 首步跳变 ↓ | q01/q99外 ↓ | Seed波动 ↓ |
|---:|---:|---:|---:|---:|---:|
| 5K | 0.026139 | 0.082520 | 0.052137 | 28.26% | 0.020977 |
| 10K | 0.020473 | 0.063973 | 0.045331 | 22.34% | 0.017570 |
| 15K | 0.018707 | 0.058234 | 0.048144 | 28.80% | 0.016876 |
| 20K | 0.017399 | 0.053620 | 0.048326 | 24.28% | 0.015630 |
| 25K | 0.017423 | 0.053288 | 0.044612 | 24.16% | 0.014249 |
| 30K | 0.015811 | 0.048148 | 0.047466 | 10.84% | 0.013018 |
| 35K | 0.014381 | 0.043644 | 0.046335 | 15.46% | 0.011715 |
| 40K | 0.013236 | 0.040604 | 0.046708 | 19.30% | 0.010086 |
| 45K | 0.012664 | 0.038463 | 0.045011 | 7.79% | 0.009192 |
| 50K | 0.011776 | 0.035677 | 0.046569 | 14.04% | 0.007686 |
| 55K | 0.011616 | 0.035015 | 0.046134 | 7.27% | 0.006835 |
| 60K | 0.011192 | 0.033842 | 0.046132 | 5.66% | 0.006093 |
| 65K | 0.010916 | 0.032989 | 0.046128 | 4.85% | 0.005800 |
| 70K | 0.010849 | 0.032814 | 0.046170 | 4.46% | 0.005652 |
| 75K | **0.010842** | **0.032774** | **0.046007** | **3.26%** | **0.005468** |

Pass A中75K点估计最好。配对CI仍无法区分65K、70K、75K，因此三者进入Pass B；
60K及以前相对75K的总MAE差异已明确。

## 7. 完整动作实验实际记录的全部指标

动作一致性：

- normalized与physical total MAE；
- left arm、right arm、left hand、right hand分组MAE；
- horizon 1、5、10、20、25、50 physical MAE。

连续性与动态：

- `first_action_jump_mae`；
- `first_action_jump_max`，即batch最大值再做episode/macro聚合；
- velocity、acceleration、jerk RMSE相对示范轨迹。

分布与随机性风险：

- `out_of_train_q01_q99_fraction`；
- `seed_dispersion_mae`；
- NaN/Inf、输出shape和动作合同门禁。

动态RMSE不是预测动作自身的硬速度限制；q01/q99外比例是训练低支持区信号，不等于
机械joint-limit违反率。

## 8. E4：65K/70K/75K密集 Pass B

协议：`80 episodes × 16固定位置 × 5 generation seeds = 6,400次完整生成/checkpoint`；
seed基数`20260820`，Euler steps 10，batch size 2。三个worker均exit 0。

### 8.1 动作与Horizon

| 指标 ↓ | 65K | 70K | 75K |
|---|---:|---:|---:|
| Physical MAE all | 0.01239149 | **0.01232576** | 0.01233291 |
| Normalized MAE all | 0.04073224 | 0.04059437 | **0.04057291** |
| Left arm | 0.01434855 | 0.01433745 | **0.01428778** |
| Right arm | 0.01551247 | 0.01555997 | **0.01547074** |
| Left hand | 0.00987616 | **0.00976261** | 0.00985905 |
| Right hand | 0.01135220 | **0.01121677** | 0.01124187 |
| Horizon 1 | 0.00556970 | **0.00552794** | 0.00556518 |
| Horizon 5 | 0.00742978 | **0.00736514** | 0.00742141 |
| Horizon 10 | 0.00996479 | **0.00988209** | 0.00990576 |
| Horizon 20 | 0.01305299 | 0.01301690 | **0.01295344** |
| Horizon 25 | 0.01437187 | 0.01434851 | **0.01429345** |
| Horizon 50 | 0.01464581 | **0.01455290** | 0.01458288 |

70K总physical MAE比75K低`0.00000715`，约0.058%，不能仅凭点估计宣布获胜。

### 8.2 稳定性与风险

| 指标 ↓ | 65K | 70K | 75K |
|---|---:|---:|---:|
| First jump mean | **0.03294916** | 0.03299066 | 0.03307546 |
| First jump batch-max mean | **0.29704811** | 0.29720265 | 0.29830809 |
| q01/q99外比例 | 5.050% | 4.170% | **3.599%** |
| Seed dispersion | 0.00644991 | 0.00628764 | **0.00610937** |
| Velocity RMSE | 0.24872081 | 0.24732233 | **0.24681235** |
| Acceleration RMSE | 6.95296 | 6.90651 | **6.89213** |
| Jerk RMSE | 318.512 | 316.151 | **315.437** |

## 9. E5：Episode配对Bootstrap

对同一80个episode的差值执行20,000次bootstrap，seed为`20260821`。差值定义为
左候选减右候选，所有指标越低越好。

### 9.1 65K与70K

- physical MAE差：`+0.00006574`；
- 95% CI：`[+0.00000902, +0.00012321]`；
- 65K episode win rate：40%；
- 65K的q01/q99外比例、seed波动和动态指标均显著更差。

结论：70K优于65K，65K退出最终双候选。

### 9.2 75K与70K

| 指标，75K−70K | Mean delta | 95% CI | 75K win rate | 判断 |
|---|---:|---:|---:|---|
| Physical MAE all | +0.00000715 | [-0.00002624,+0.00003997] | 45.0% | 并列 |
| Normalized MAE | -0.00002146 | [-0.00012506,+0.00008235] | 47.5% | 并列 |
| Left arm | -0.00004967 | [-0.00009384,-0.00000647] | 58.8% | 75K显著更好 |
| Right arm | -0.00008923 | [-0.00015199,-0.00002451] | 67.5% | 75K显著更好 |
| Left hand | +0.00009644 | [+0.00003115,+0.00016060] | 40.0% | 70K显著更好 |
| First jump mean | +0.00008480 | [+0.00003699,+0.00013393] | 40.0% | 70K显著更好 |
| First jump max | +0.00110543 | [+0.00049287,+0.00174578] | 38.8% | 70K显著更好 |
| q01/q99外比例 | -0.00571618 | [-0.00637289,-0.00505790] | 97.5% | 75K显著更好 |
| Seed dispersion | -0.00017827 | [-0.00019381,-0.00016390] | 100% | 75K显著更好 |
| Velocity RMSE | -0.00052272 | [-0.00103060,-0.00002252] | 58.8% | 75K显著更好 |
| Acceleration RMSE | -0.01270947 | [-0.02639204,+0.00114391] | 60.0% | 未区分 |
| Jerk RMSE | -0.61800044 | [-1.39050125,+0.16179307] | 62.5% | 未区分 |

## 10. 当前Primary/Fallback决策

- 当前离线`Primary：75K`；
- 当前离线`Fallback：70K`；
- 65K保留证据但退出最终双候选。

选择75K不是因为密集Pass B总MAE最低；该指标最低的是70K且二者统计并列。选择75K
是因为当前主要真机问题发生在瓶子偏位后的双臂到达，而75K显著降低双臂误差、训练
分位范围外输出和seed波动。70K首步切换和左手更好，保留为Fallback。

当前不能声称75K真机成功率一定更高、已经学会抓空重试、一定依赖视觉，或者不会发生
机械限位/碰撞。这些必须由后续实验回答。

## 11. 尚未覆盖的安全指标与后续实验

1. 每关节首动作跳变P95/P99/全局最大值；
2. 预测轨迹自身velocity/acceleration/jerk的P95/P99/最大值；
3. G2真实mechanical joint limits违反率；
4. 左右臂自碰撞与环境碰撞检查；
5. 历史抓空后新观测的动作阶段分类；
6. 原图、光照变化、黑图、错配图、时序打乱图的paired视觉敏感性；
7. powered-sync 20/50下真机抓取、交接、放置、恢复和急停率。

## 12. 产物与复现路径

代码：

- `g2_sort/validation/newlora_common.py`；
- `g2_sort/validation/evaluate_NewLora_fm.py`；
- `g2_sort/validation/evaluate_NewLora_actions.py`；
- `g2_sort/validation/summarize_NewLora_validation.py`；
- `g2_sort/validation/summarize_NewLora_passB.py`；
- `infra/host/run_NewLora_validation_4gpu.sh`。

结果：

```text
server/evaluations/NewLora_validation_v1/
├── manifests/
├── results/fm/checkpoint-*.json
├── results/actions/checkpoint-*.json
├── results/actions_passB/checkpoint-{65000,70000,75000}.json
├── logs/
├── summary.json
└── passB_paired_summary.json
```

每份JSON保留macro和80个episode级指标，不只保留最终排行榜。
