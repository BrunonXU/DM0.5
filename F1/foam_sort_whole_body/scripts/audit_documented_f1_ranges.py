#!/usr/bin/env python3
"""Read-only comparison against the supplied F1 interface document, not a filter."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


ARM_NAMES = [f"arm_{side}_j{i}" for side in ("l", "r") for i in range(1, 8)]
LEAD_NAMES = [f"arm_{side}_joint{i}" for side in ("L", "R") for i in range(1, 8)]
LOW = [-178, -120, -178, -145, -178, -60, -90] * 2
HIGH = [178, 120, 178, 60, 178, 60, 90] * 2


def fingerprint(path):
    s = path.stat()
    return {"size": s.st_size, "mtime_ns": s.st_mtime_ns}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    task_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--signals", type=Path, default=task_root / "results/cleaning_v1/signals")
    parser.add_argument("--document", type=Path, default=task_root.parent / "F1系统接口清单.md")
    parser.add_argument("--output", type=Path, default=task_root / "results/interface_review_20260914")
    args = parser.parse_args()
    specs = {
        "lead_q": (LEAD_NAMES, LOW, HIGH),
        "hal_q": (ARM_NAMES, LOW, HIGH),
        **{key: ([key], [0], [100]) for key in ("left_cmd", "right_cmd", "left_state", "right_state")},
    }
    streams = {}
    for key, (names, low, high) in specs.items():
        streams[key] = {
            "samples": 0, "out_of_document_range_samples": 0,
            "episodes_out_of_document_range": [], "fields": [
                {"field": name, "document_min": lo, "document_max": hi,
                 "min": None, "max": None, "nonfinite_points": 0,
                 "below_document_min_points": 0, "above_document_max_points": 0,
                 "out_of_document_range_points": 0,
                 "episodes_out_of_document_range": [], "max_excess": 0.0,
                 "max_excess_example": None}
                for name, lo, hi in zip(names, low, high)
            ],
        }
    paths = sorted(args.signals.glob("*/*.npz"))
    if not paths:
        raise RuntimeError("No extracted signal packs")
    for path in paths:
        meta_path = path.with_suffix(".json")
        metadata = json.loads(meta_path.read_text())
        before = fingerprint(path)
        if metadata["status"] != "complete" or before != metadata["npz_fingerprint"]:
            raise ValueError(f"Uncommitted or changed signal pack: {path}")
        episode = path.relative_to(args.signals).with_suffix("").as_posix()
        if metadata["episode"] != episode:
            raise ValueError(f"Identity mismatch: {path}")
        if metadata["column_order"]["lead_q"] != LEAD_NAMES or metadata["column_order"]["hal_q"][:14] != ARM_NAMES:
            raise ValueError(f"Column mismatch: {path}")
        with np.load(path, allow_pickle=False) as pack:
            for key, (names, low, high) in specs.items():
                values = pack[key]
                values = values[:, :14] if key in ("hal_q", "lead_q") else values[:, None]
                if values.shape[1] != len(names):
                    raise ValueError(f"Shape mismatch: {path}:{key}")
                finite = np.isfinite(values)
                below = finite & (values < np.asarray(low))
                above = finite & (values > np.asarray(high))
                outside = below | above
                stream = streams[key]
                stream["samples"] += len(values)
                stream["out_of_document_range_samples"] += int(np.any(outside, axis=1).sum())
                if outside.any():
                    stream["episodes_out_of_document_range"].append(episode)
                for col, field in enumerate(stream["fields"]):
                    v = values[:, col]
                    vf = v[finite[:, col]]
                    if len(vf):
                        minimum, maximum = float(vf.min()), float(vf.max())
                        field["min"] = minimum if field["min"] is None else min(field["min"], minimum)
                        field["max"] = maximum if field["max"] is None else max(field["max"], maximum)
                    field["nonfinite_points"] += int((~finite[:, col]).sum())
                    field["below_document_min_points"] += int(below[:, col].sum())
                    field["above_document_max_points"] += int(above[:, col].sum())
                    field["out_of_document_range_points"] += int(outside[:, col].sum())
                    if outside[:, col].any():
                        field["episodes_out_of_document_range"].append(episode)
                        excess = np.where(finite[:, col], np.maximum(low[col] - v, v - high[col]), 0)
                        index = int(excess.argmax())
                        if excess[index] > field["max_excess"]:
                            time_key = ("lead" if key == "lead_q" else "hal" if key == "hal_q" else key) + "_t_ns"
                            field["max_excess"] = float(excess[index])
                            field["max_excess_example"] = {
                                "episode": episode, "sample_index": index, "value": float(v[index]),
                                "mcap_log_time_ns": int(pack[time_key][index]),
                                "relative_to_record_start_s": float((int(pack[time_key][index]) - metadata["start_ns"]) / 1e9),
                            }
        if fingerprint(path) != before:
            raise ValueError(f"Signal pack changed while reading: {path}")
    for stream in streams.values():
        stream["episodes_out_of_document_range_count"] = len(stream["episodes_out_of_document_range"])
        for field in stream["fields"]:
            field["episodes_out_of_document_range_count"] = len(field["episodes_out_of_document_range"])
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_kind": "documented_range_comparison_only_not_training_acceptance",
        "source_signals": str(args.signals.resolve()), "episode_count": len(paths),
        "document": str(args.document.resolve()),
        "document_sha256": hashlib.sha256(args.document.read_bytes()).hexdigest(),
        "document_sections": {"arms": "0608 upgrade, lines 1515–1543", "grippers": "3.7/3.8, lines 682–800"},
        "arm_comparison_assumption": "raw recorded arm positions are compared numerically to documented degrees; installed driver/version and /lead actuator semantics require confirmation",
        "document_version_conflicts": "0608 upgrade lists J1/J3/J5 ±178 degrees, J2 ±120; IK examples also use ±170 and some J2 minima −94 instead of −120",
        "gripper_definition": "0 fully open, 100 closed; this is a 0–100 position scale, not a conversion to physical degrees",
        "comparison_tolerance": 0,
        "recorded_values_modified": False, "cleaning_rules_modified": False,
        "training_rejection_applied": False, "streams": streams,
        "interpretation": [
            "Out-of-document-range counts are audit clues, not proof that a recording is unsafe or corrupt.",
            "A lead target and measured hal feedback can differ because of tracking lag, command interpolation, saturation, calibration, or topic semantics; no rejection threshold is applied here.",
            "Recorded feedback extrema do not determine controller command limits, velocity limits, or acceleration limits.",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "documented_range_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# F1 文档限位与全量记录对照（2026-09-14）", "",
        f"只读核对 {len(paths)} 条信号包，未裁剪数值、未改清洗规则、未据此拒收训练数据。", "",
        "对照来源：[F1 系统接口清单](../../../F1系统接口清单.md)，0608 升级段落：每臂 J1 ±178、J2 ±120、J3 ±178、J4 [−145,60]、J5 ±178、J6 ±60、J7 ±90（度）。夹爪 0 全开、100 闭合，记录中的 0–100 是位置标度，不能解释为物理角度。", "",
        "文档版本存在差异：IK 示例的 J1/J3/J5 为 ±170，部分 J2 下限为 −94。本表只对照 0608 段落；实际采集/回放驱动的版本和限位仍须核对。对 `/lead` 的数值范围对照以记录值采用度为假设，尚不证明它等价于最终执行器命令。", "",
        "| 来源 | 全量样本数 | 有任一字段越文档范围的样本 | 涉及原始记录 |", "| --- | ---: | ---: | ---: |",
    ]
    for key, stream in streams.items():
        lines.append(f"| {key} | {stream['samples']:,} | {stream['out_of_document_range_samples']:,} | {stream['episodes_out_of_document_range_count']} |")
    lines.extend(["", "| 来源/字段 | 记录最小值 | 记录最大值 | 文档范围 | 越界点数 | 涉及记录数 | 最大超出 |", "| --- | ---: | ---: | --- | ---: | ---: | ---: |"])
    for key, stream in streams.items():
        for f in stream["fields"]:
            lines.append(f"| {key}/{f['field']} | {f['min']:.6f} | {f['max']:.6f} | [{f['document_min']}, {f['document_max']}] | {f['out_of_document_range_points']:,} | {f['episodes_out_of_document_range_count']} | {f['max_excess']:.6f} |")
    j4 = streams["lead_q"]["fields"][3]
    j6 = streams["lead_q"]["fields"][5]
    lines.extend([
        "", f"本批需要分开解释两类差异：左臂 J4 在 {j4['episodes_out_of_document_range_count']} 条记录中严格低于文档下限，最大仅 {j4['max_excess']:.6f}°，属于很小的边界差异，不能把这些记录直接计为坏数据。左臂 J6 在 {j6['episodes_out_of_document_range_count']} 条记录中高于 60°，共 {j6['out_of_document_range_points']} 个目标点、最大 {j6['max']:.6f}°；这部分需要核对 lead 目标经过何种限位或变换才到执行器，再决定训练标签如何定义。", "",
        "左臂 J6 待核对来源：" + "、".join(f"`{episode}`" for episode in j6["episodes_out_of_document_range"]) + "。", "",
        "双臂 hal 反馈没有越过本次对照的文档范围；夹爪命令和反馈均在 0–100，右夹爪命令与反馈全程为 100，对照新文档应解释为闭合。",
    ])
    lines.extend(["", "最大超出对应的记录、样本索引和时间，以及全部涉及记录，见 [JSON](documented_range_audit.json)。所有点采用严格数值比较，未加容差；小幅越界也会计数。", "",
                  "lead 目标与 hal 实测反馈的同时时刻差异，可能来自执行滞后、插值、饱和、标定或 Topic 语义，不能单凭差值自动删数据。反馈历史极值也不能代替真机的速度、加速度和控制限位验收。", "",
                  "本检查证明数值相对于这版文档的关系，不证明动作成功、相机曝光同步、轨迹可执行或真机 replay 通过。"])
    (args.output / "documented_range_audit.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"episode_count": len(paths), "streams": {key: {k: value for k, value in stream.items() if k != "fields" and k != "episodes_out_of_document_range"} for key, stream in streams.items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
