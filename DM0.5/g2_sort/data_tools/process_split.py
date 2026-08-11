#!/usr/bin/env python3
"""按照冻结的 split manifest 批量审计并转换已解压 episode。

输入 split manifest和episode根目录，逐条调用审计与转换工具并记录失败原因；
任何失败都会返回非零状态，禁止静默混入训练集。本文件属于 G2项目定制代码。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from g2_sort.data_tools.audit_episode import audit_episode
from g2_sort.data_tools.convert_episode import convert_episode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("split_manifest", type=Path)
    parser.add_argument("extracted_root", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--remux", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.split_manifest.read_text())
    failures = []
    converted = 0
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    for split in ("train", "validation"):
        for episode_id in manifest.get(split, []):
            episode_dir = args.extracted_root / episode_id
            report = audit_episode(episode_dir)
            audit_path = args.audit_dir / f"{episode_id}.json"
            audit_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            if not report["valid"]:
                failures.append(
                    {
                        "episode": episode_id,
                        "split": split,
                        "errors": report["errors"],
                    }
                )
                continue
            if not args.audit_only:
                convert_episode(
                    episode_dir,
                    args.output_root,
                    split,
                    args.fps,
                    args.remux,
                )
                converted += 1

    summary = {
        "converted_episode_count": converted,
        "failed_episode_count": len(failures),
        "failures": failures,
    }
    summary_path = args.audit_dir / "processing_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(
        f"converted={converted} failed={len(failures)} "
        f"summary={summary_path}"
    )
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
