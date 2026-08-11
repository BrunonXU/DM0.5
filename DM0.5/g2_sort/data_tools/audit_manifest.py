#!/usr/bin/env python3
"""并行完整审计提取后的全量episode，并生成汇总报告。"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from g2_sort.data_tools.audit_episode import audit_episode


def _audit_one(episode_id: str, episode_dir: Path, output: Path) -> dict:
    try:
        report = audit_episode(episode_dir)
    except Exception as exc:
        report = {
            "episode": episode_id,
            "valid": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("episode_root", type=Path)
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    if args.workers <= 0:
        parser.error("--workers must be positive")

    manifest = json.loads(args.manifest.read_text())
    episode_ids = [item["episode"] for item in manifest["selected"]]
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    pending = []
    for episode_id in episode_ids:
        output = args.audit_dir / f"{episode_id}.json"
        if output.exists():
            reports.append(json.loads(output.read_text()))
        else:
            pending.append((episode_id, args.episode_root / episode_id, output))

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_audit_one, episode_id, episode_dir, output): episode_id
            for episode_id, episode_dir, output in pending
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            report = future.result()
            reports.append(report)
            if completed % 25 == 0 or completed == len(futures):
                print(
                    f"audited={completed}/{len(futures)} "
                    f"latest={report['episode']} valid={report['valid']}",
                    flush=True,
                )

    valid = sorted(report["episode"] for report in reports if report.get("valid"))
    invalid = sorted(
        (
            {
                "episode": report.get("episode", "<unknown>"),
                "errors": report.get("errors", []),
            }
            for report in reports
            if not report.get("valid")
        ),
        key=lambda item: item["episode"],
    )
    summary = {
        "requested_episode_count": len(episode_ids),
        "audited_episode_count": len(reports),
        "valid_episode_count": len(valid),
        "invalid_episode_count": len(invalid),
        "valid": valid,
        "invalid": invalid,
    }
    summary_path = args.audit_dir / "audit_summary.json"
    temporary = summary_path.with_name(f".{summary_path.name}.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(summary_path)
    print(
        f"valid={len(valid)} invalid={len(invalid)} summary={summary_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
