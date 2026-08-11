#!/usr/bin/env python3
"""合并多个归档索引，去重并生成确定性的必需成员提取清单。"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def build_manifest(index_paths: list[Path]) -> dict:
    candidates: dict[str, list[dict]] = defaultdict(list)
    excluded = []
    archive_summaries = []
    for priority, index_path in enumerate(index_paths):
        report = json.loads(index_path.read_text())
        archive = str(Path(report["archive"]).resolve())
        archive_summaries.append(
            {
                "index": str(index_path.resolve()),
                "archive": archive,
                "episode_count": report["episode_count"],
                "precheck_valid_count": report["precheck_valid_count"],
            }
        )
        for episode in report["episodes"]:
            item = {
                **episode,
                "archive": archive,
                "source_index": str(index_path.resolve()),
                "priority": priority,
            }
            if episode["precheck_valid"]:
                candidates[episode["episode"]].append(item)
            else:
                excluded.append(item)

    selected = []
    duplicates = []
    for episode_id in sorted(candidates):
        group = sorted(candidates[episode_id], key=lambda item: item["priority"])
        chosen = group[-1]  # Later CLI indexes have explicit precedence.
        selected.append(chosen)
        if len(group) > 1:
            duplicates.append(
                {
                    "episode": episode_id,
                    "selected_archive": chosen["archive"],
                    "alternative_archives": [
                        item["archive"] for item in group[:-1]
                    ],
                }
            )

    return {
        "schema_version": 1,
        "selection_rule": (
            "precheck_valid episodes only; duplicate episode IDs use the "
            "last index supplied on the command line"
        ),
        "archives": archive_summaries,
        "raw_episode_count": sum(item["episode_count"] for item in archive_summaries),
        "selected_episode_count": len(selected),
        "excluded_record_count": len(excluded),
        "duplicate_episode_count": len(duplicates),
        "selected_by_archive": dict(
            Counter(item["archive"] for item in selected)
        ),
        "selected": selected,
        "excluded": excluded,
        "duplicates": duplicates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("indexes", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.indexes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    temporary.replace(args.output)
    print(
        f"selected={manifest['selected_episode_count']} "
        f"excluded_records={manifest['excluded_record_count']} "
        f"duplicate_ids={manifest['duplicate_episode_count']}"
    )


if __name__ == "__main__":
    main()
