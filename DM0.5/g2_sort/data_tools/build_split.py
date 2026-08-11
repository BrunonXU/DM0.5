#!/usr/bin/env python3
"""根据episode审计报告生成确定性的分层train/validation划分。

只接受审计通过的完整 episode，并按 AID、软件版本和 task 分层；输出 split
manifest，保证同一 episode 不会跨集合。该工具不读取视频。本文件属于 G2
项目定制代码。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path


def _stable_rank(seed: str, episode: str) -> str:
    return hashlib.sha256(f"{seed}|{episode}".encode()).hexdigest()


def build_split(
    reports: list[dict],
    validation_ratio: float,
    seed: str,
    train_count: int | None = None,
) -> dict:
    valid = [report for report in reports if report.get("valid")]
    if train_count is not None and not 0 < train_count < len(valid):
        raise ValueError(
            f"train_count must be between 1 and {len(valid) - 1}, got {train_count}"
        )
    strata = defaultdict(list)
    for report in valid:
        key = (
            report.get("aid"),
            report.get("software_version"),
            report.get("task_id"),
        )
        strata[key].append(report)

    keys = sorted(strata, key=str)
    target_validation_count = (
        len(valid) - train_count
        if train_count is not None
        else None
    )
    allocations: dict[tuple, int] = {}
    if target_validation_count is not None:
        minimums = {
            key: 1 if len(strata[key]) >= 2 else 0
            for key in keys
        }
        if sum(minimums.values()) > target_validation_count:
            minimums = {key: 0 for key in keys}
        ideals = {
            key: len(strata[key]) * target_validation_count / len(valid)
            for key in keys
        }
        allocations = {
            key: min(
                len(strata[key]) - 1,
                max(minimums[key], math.floor(ideals[key])),
            )
            for key in keys
        }
        while sum(allocations.values()) < target_validation_count:
            candidates = [
                key
                for key in keys
                if allocations[key] < len(strata[key]) - 1
            ]
            if not candidates:
                raise ValueError("cannot allocate requested validation set by strata")
            key = max(
                candidates,
                key=lambda item: (ideals[item] - allocations[item], str(item)),
            )
            allocations[key] += 1
        while sum(allocations.values()) > target_validation_count:
            candidates = [
                key
                for key in keys
                if allocations[key] > minimums[key]
            ]
            key = min(
                candidates,
                key=lambda item: (ideals[item] - allocations[item], str(item)),
            )
            allocations[key] -= 1

    train, validation = [], []
    for key in keys:
        group = sorted(
            strata[key], key=lambda item: _stable_rank(seed, item["episode"])
        )
        count = len(group)
        if target_validation_count is not None:
            val_count = allocations[key]
        else:
            val_count = 0 if count < 2 else max(1, round(count * validation_ratio))
            val_count = min(val_count, count - 1)
        validation.extend(item["episode"] for item in group[:val_count])
        train.extend(item["episode"] for item in group[val_count:])

    return {
        "seed": seed,
        "validation_ratio_requested": validation_ratio,
        "train_count_requested": train_count,
        "train": sorted(train),
        "validation": sorted(validation),
        "excluded": sorted(
            report.get("episode", "<unknown>")
            for report in reports
            if not report.get("valid")
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validation-ratio", type=float, default=0.05)
    parser.add_argument("--train-count", type=int)
    parser.add_argument("--seed", default="dm05-g2-v1")
    args = parser.parse_args()
    if not 0 < args.validation_ratio < 1:
        parser.error("--validation-ratio must be between 0 and 1")
    reports = [
        report
        for path in sorted(args.audit_dir.glob("*.json"))
        if (report := json.loads(path.read_text())).get("episode")
        and isinstance(report.get("valid"), bool)
    ]
    try:
        manifest = build_split(
            reports,
            args.validation_ratio,
            args.seed,
            args.train_count,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        f"train={len(manifest['train'])} validation={len(manifest['validation'])} "
        f"excluded={len(manifest['excluded'])}"
    )


if __name__ == "__main__":
    main()
