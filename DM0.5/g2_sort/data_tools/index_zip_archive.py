#!/usr/bin/env python3
"""轻量索引 G2 ZIP 中全部 episode 元数据和必需文件。

读取 ZIP 中央目录及每个 ``meta_info.json``，输出归档级统计和 episode 索引；
不提取或解码视频，适合先判断大归档的可用范围。本文件属于 G2项目定制代码。
"""

from __future__ import annotations

import argparse
import json
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath


def index_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        meta_members = sorted(
            name for name in members if name.endswith("/meta_info.json")
        )
        episodes = []
        for meta_member in meta_members:
            episode_root = PurePosixPath(meta_member).parent
            try:
                meta = json.loads(archive.read(meta_member))
                metadata_error = None
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                meta = {}
                metadata_error = str(exc)

            required = {
                "h5": str(episode_root / "record/aligned_joints.h5"),
                "head": str(episode_root / "camera/head_color/head_color.h265"),
                "left_wrist": str(
                    episode_root / "camera/hand_left_color/hand_left_color.h265"
                ),
                "right_wrist": str(
                    episode_root / "camera/hand_right_color/hand_right_color.h265"
                ),
                "head_timestamps": str(
                    episode_root / "camera/head_color/head_color.txt"
                ),
                "left_wrist_timestamps": str(
                    episode_root / "camera/hand_left_color/hand_left_color.txt"
                ),
                "right_wrist_timestamps": str(
                    episode_root
                    / "camera/hand_right_color/hand_right_color.txt"
                ),
            }
            presence = {key: value in members for key, value in required.items()}
            validation = meta.get("data_validate", {}).get("validate")
            integrity = meta.get("integrity", {}).get("integrity")
            duration = meta.get("duration")
            reasons = []
            if metadata_error:
                reasons.append("invalid_meta_json")
            if validation is False:
                reasons.append("data_validate_false")
            if integrity is False:
                reasons.append("integrity_false")
            if meta.get("fps_validate") is False:
                reasons.append("fps_validate_false")
            if isinstance(duration, (int, float)) and duration >= 600:
                reasons.append("duration_at_least_600s")
            reasons.extend(f"missing_{key}" for key, exists in presence.items() if not exists)
            episodes.append(
                {
                    "episode": episode_root.name,
                    "archive_member_root": str(episode_root),
                    "aid": meta.get("AID"),
                    "software_version": meta.get("sw_version"),
                    "task_id": meta.get("task_id"),
                    "duration_seconds": duration,
                    "metadata_valid": validation,
                    "integrity": integrity,
                    "fps_valid": meta.get("fps_validate"),
                    "required_members": presence,
                    "precheck_valid": not reasons,
                    "exclusion_reasons": reasons,
                }
            )

    def counts(field: str) -> dict:
        return dict(Counter(str(item.get(field)) for item in episodes))

    return {
        "archive": str(path),
        "archive_size_bytes": path.stat().st_size,
        "method": "full ZIP central-directory and meta_info index; no video decoding",
        "episode_count": len(episodes),
        "precheck_valid_count": sum(item["precheck_valid"] for item in episodes),
        "counts": {
            "aid": counts("aid"),
            "software_version": counts("software_version"),
            "task_id": counts("task_id"),
            "exclusion_reason": dict(
                Counter(
                    reason
                    for item in episodes
                    for reason in item["exclusion_reasons"]
                )
            ),
        },
        "episodes": episodes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = index_zip(args.archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        f"episodes={report['episode_count']} "
        f"precheck_valid={report['precheck_valid_count']}"
    )


if __name__ == "__main__":
    main()
