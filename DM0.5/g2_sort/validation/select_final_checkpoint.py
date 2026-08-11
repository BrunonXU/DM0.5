#!/usr/bin/env python3
"""Select primary/fallback checkpoints and render the frozen decision report."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def percent(value: float) -> str:
    return f"{value * 100:.4f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round1-summary", required=True)
    parser.add_argument("--round2-summary", required=True)
    parser.add_argument("--action-dir", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args()

    round1 = json.loads(Path(args.round1_summary).read_text(encoding="utf-8"))
    round2 = json.loads(Path(args.round2_summary).read_text(encoding="utf-8"))
    round2_by_checkpoint = {
        row["checkpoint"]: row for row in round2["ranking"]
    }
    finalists = []
    for path in sorted(Path(args.action_dir).glob("checkpoint-*.json")):
        checkpoint = int(path.stem.split("-")[-1])
        action = json.loads(path.read_text(encoding="utf-8"))
        finalists.append(
            {
                "checkpoint": checkpoint,
                "round2": round2_by_checkpoint[checkpoint],
                "action": action,
            }
        )
    if len(finalists) != 2:
        raise RuntimeError(f"expected exactly 2 action finalists, found {len(finalists)}")

    # Deployment-path generated action fidelity is the final primary metric.
    # Round-2 FM loss and episode P90 break ties, followed by the earlier model.
    finalists.sort(
        key=lambda item: (
            item["action"]["normalized_mae_by_group"]["all"],
            item["action"]["episode_p90_physical_mae"],
            item["round2"]["episode_macro_mean_loss"],
            item["checkpoint"],
        )
    )
    primary, fallback = finalists
    decision = {
        "selection_rule": (
            "lowest deployment-path normalized all-joint action MAE; "
            "then action episode P90, round-2 FM loss, earlier checkpoint"
        ),
        "primary_checkpoint": primary["checkpoint"],
        "fallback_checkpoint": fallback["checkpoint"],
        "finalists": finalists,
    }
    Path(args.output_json).write_text(
        json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    round1_rows = "\n".join(
        f"| {row['checkpoint']:,} | {row['episode_macro_mean_loss']:.8f} | "
        f"{row['episode_macro_median_loss']:.8f} | "
        f"{row['episode_macro_p90_loss']:.8f} |"
        for row in round1["ranking"]
    )
    round2_rows = "\n".join(
        f"| {row['checkpoint']:,} | {row['episode_macro_mean_loss']:.8f} | "
        f"{row['episode_macro_median_loss']:.8f} | "
        f"{row['episode_macro_p90_loss']:.8f} |"
        for row in round2["ranking"]
    )
    action_rows = "\n".join(
        f"| {item['checkpoint']:,} | "
        f"{item['action']['normalized_mae_by_group']['all']:.8f} | "
        f"{item['action']['episode_macro_physical_mae']:.8f} | "
        f"{item['action']['episode_p90_physical_mae']:.8f} | "
        f"{item['action']['first_action_jump_mae']:.8f} | "
        f"{percent(item['action']['out_of_train_q01_q99_fraction'])} | "
        f"{item['action']['seed_dispersion_mae']:.8f} |"
        for item in finalists
    )
    primary_action = primary["action"]
    fallback_action = fallback["action"]
    markdown = f"""# DM0.5 × G2 Checkpoint Validation and Selection Decision

更新时间：{datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")}

## 1. 决策结论

- Primary checkpoint：`checkpoint-{primary['checkpoint']}`
- Fallback checkpoint：`checkpoint-{fallback['checkpoint']}`
- 决策规则：{decision['selection_rule']}。

Primary用于后续离线client合同检查和shadow mode；Fallback必须保留，出现输出
跳变、越界、时延或真机稳定性退化时立即回退。本结论只覆盖离线validation，
不等于真机成功率证明。

## 2. 冻结候选与数据边界

- 候选：25K、30K、35K、40K、45K、50K；
- 数据：训练前冻结的80个validation episode，与800条train无episode交叉；
- normalization：始终复用train-only 34D统计；
- attention：LLM SDPA / Vision FlashAttention2 / Action SDPA；
- 动作合同：absolute、50×34、30Hz数据时间语义；
- 所有候选使用相同采样位置、batch分组、flow seed和生成seed。

## 3. 第一轮FM Loss筛选

每episode等距16帧、3轮固定flow随机性，共1,280个唯一帧和3,840次样本曝光/
checkpoint。按episode先聚合再macro average，避免长episode支配结果。

| Checkpoint | Macro mean | Macro median | Episode P90 |
|---:|---:|---:|---:|
{round1_rows}

入围第二轮：{", ".join(f"`checkpoint-{x}`" for x in round1["selected_checkpoints"])}。

## 4. 第二轮加密FM Loss

第一轮前三名使用每episode 32帧、5轮固定flow随机性复测，共2,560个唯一帧和
12,800次样本曝光/checkpoint。

| Checkpoint | Macro mean | Macro median | Episode P90 |
|---:|---:|---:|---:|
{round2_rows}

进入完整动作生成比较：
{", ".join(f"`checkpoint-{x}`" for x in round2["selected_checkpoints"])}。

## 5. 部署路径完整动作生成

最终两名使用与部署一致的10步Euler flow生成`50×34`动作。每episode等距4帧、
2个固定生成seed。指标在反归一化后计算，同时保留归一化全关节MAE作为不同关节
尺度之间的公平主指标。

| Checkpoint | Normalized MAE | Physical macro MAE | Physical P90 | First-action jump | q01/q99外比例 | Seed dispersion |
|---:|---:|---:|---:|---:|---:|---:|
{action_rows}

Primary分组physical MAE：

- left arm：{primary_action['physical_mae_by_group']['left_arm']:.8f}
- right arm：{primary_action['physical_mae_by_group']['right_arm']:.8f}
- left hand：{primary_action['physical_mae_by_group']['left_hand']:.8f}
- right hand：{primary_action['physical_mae_by_group']['right_hand']:.8f}

Primary horizon physical MAE：

- step 1：{primary_action['physical_mae_by_horizon']['1']:.8f}
- step 5：{primary_action['physical_mae_by_horizon']['5']:.8f}
- step 10：{primary_action['physical_mae_by_horizon']['10']:.8f}
- step 25：{primary_action['physical_mae_by_horizon']['25']:.8f}
- step 50：{primary_action['physical_mae_by_horizon']['50']:.8f}

## 6. 为什么选择Primary

最终排序首先看部署路径生成动作的归一化全关节MAE，而不是只看train loss或
单次FM loss。Primary的该指标为
`{primary_action['normalized_mae_by_group']['all']:.8f}`，Fallback为
`{fallback_action['normalized_mae_by_group']['all']:.8f}`。随后使用动作episode
P90、第二轮FM loss和更早checkpoint依次消除并列。

这使决策同时覆盖训练目标、未见episode泛化和实际10步生成器输出，但仍不能
覆盖闭环误差累积、控制时延、机器人动力学和场景分布漂移。

## 7. 必要后续门禁

1. 全新进程加载Primary与Fallback，确认输出始终为有限`50×34`；
2. 固定34D joint order、absolute语义、单位和30Hz policy时间含义；
3. client离线回放并检查限位、速度、加速度、watchdog和safe hold；
4. 真机shadow只记录不执行；
5. 低速、可急停、短前缀powered test；
6. 完整任务重复试验后才允许宣布闭环完成。

50K→100K属于独立延长实验。它不会追溯改变本文件的25K–50K决策；只有后续
checkpoint通过完全相同协议并实质优于Primary，才发布新的决策版本。
"""
    Path(args.output_md).write_text(markdown, encoding="utf-8")
    print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    main()
