#!/usr/bin/env python3
"""单遍流式索引 G2 tar.gz 中的 episode 元数据和必需文件。

使用 tar 的流式模式顺序读取归档，只解析 ``meta_info.json``，不解压 H5
或视频。输出 schema 与 ``index_zip_archive.py`` 保持一致，便于后续合并
三个来源归档。本文件属于 G2 项目定制代码。
"""

from __future__ import annotations

import argparse
import json
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath


REQUIRED_MEMBERS = {
    "h5": PurePosixPath("record/aligned_joints.h5"),
    "head": PurePosixPath("camera/head_color/head_color.h265"),
    "left_wrist": PurePosixPath(
        "camera/hand_left_color/hand_left_color.h265"
    ),
    "right_wrist": PurePosixPath(
        "camera/hand_right_color/hand_right_color.h265"
    ),
    "head_timestamps": PurePosixPath(
        "camera/head_color/head_color.txt"
    ),
    "left_wrist_timestamps": PurePosixPath(
        "camera/hand_left_color/hand_left_color.txt"
    ),
    "right_wrist_timestamps": PurePosixPath(
        "camera/hand_right_color/hand_right_color.txt"
    ),
}
META_SUFFIX = PurePosixPath("meta_info.json")
MAX_META_BYTES = 4 * 1024 * 1024


def _root_for_suffix(
    member_path: PurePosixPath, suffix: PurePosixPath
) -> PurePosixPath | None:
    member_parts = member_path.parts
    suffix_parts = suffix.parts
    if len(member_parts) <= len(suffix_parts):
        return None
    if member_parts[-len(suffix_parts) :] != suffix_parts:
        return None
    return PurePosixPath(*member_parts[: -len(suffix_parts)])


def _new_episode(root: PurePosixPath) -> dict:
    return {
        "episode": root.name,
        "archive_member_root": str(root),
        "meta": {},
        "metadata_error": None,
        "required_members": {key: False for key in REQUIRED_MEMBERS},
    }


def _finalize_episode(item: dict) -> dict:
    meta = item.pop("meta")
    metadata_error = item.pop("metadata_error")
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
    reasons.extend(
        f"missing_{key}"
        for key, exists in item["required_members"].items()
        if not exists
    )
    item.update(
        {
            "aid": meta.get("AID"),
            "software_version": meta.get("sw_version"),
            "task_id": meta.get("task_id"),
            "duration_seconds": duration,
            "metadata_valid": validation,
            "integrity": integrity,
            "fps_valid": meta.get("fps_validate"),
            "precheck_valid": not reasons,
            "exclusion_reasons": reasons,
        }
    )
    return item


def index_tar(path: Path, max_episodes: int | None = None) -> dict:
    """Index a gzip-compressed tar in one sequential pass.

    ``max_episodes`` is intended for fast sampling. The scan stops only after
    the requested number of metadata-bearing episode roots have been fully
    passed in archive order, so all members belonging to the last sampled
    episode are still observed.
    """

    episodes_by_root: dict[str, dict] = {}
    meta_roots: list[str] = []
    last_seen_root: str | None = None
    stopped_early = False

    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            member_path = PurePosixPath(member.name.lstrip("./"))
            matched_root: PurePosixPath | None = None
            matched_key: str | None = None

            meta_root = _root_for_suffix(member_path, META_SUFFIX)
            if meta_root is not None:
                matched_root = meta_root
                matched_key = "meta"
            else:
                for key, suffix in REQUIRED_MEMBERS.items():
                    root = _root_for_suffix(member_path, suffix)
                    if root is not None:
                        matched_root = root
                        matched_key = key
                        break

            if matched_root is None:
                continue
            root_name = str(matched_root)
            if (
                max_episodes is not None
                and len(meta_roots) >= max_episodes
                and root_name != last_seen_root
                and root_name not in episodes_by_root
            ):
                stopped_early = True
                break

            item = episodes_by_root.setdefault(
                root_name, _new_episode(matched_root)
            )
            last_seen_root = root_name
            if matched_key == "meta":
                if root_name not in meta_roots:
                    meta_roots.append(root_name)
                extracted = archive.extractfile(member)
                if extracted is None:
                    item["metadata_error"] = "meta_info.json is not a file"
                    continue
                raw = extracted.read(MAX_META_BYTES + 1)
                if len(raw) > MAX_META_BYTES:
                    item["metadata_error"] = (
                        f"meta_info.json exceeds {MAX_META_BYTES} bytes"
                    )
                    continue
                try:
                    item["meta"] = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    item["metadata_error"] = str(exc)
            else:
                item["required_members"][matched_key] = True

    episodes = [
        _finalize_episode(episodes_by_root[root])
        for root in meta_roots[:max_episodes]
    ]

    def counts(field: str) -> dict:
        return dict(Counter(str(item.get(field)) for item in episodes))

    return {
        "archive": str(path),
        "archive_size_bytes": path.stat().st_size,
        "method": (
            "single-pass streaming tar.gz member and meta_info index; "
            "no H5/video extraction or decoding"
        ),
        "stopped_early": stopped_early,
        "episode_count": len(episodes),
        "precheck_valid_count": sum(
            item["precheck_valid"] for item in episodes
        ),
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


def _write_json_atomic(output: Path, report: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-episodes",
        type=int,
        help="只顺序索引前 N 个 episode，用于快速抽样",
    )
    args = parser.parse_args()
    if args.max_episodes is not None and args.max_episodes <= 0:
        parser.error("--max-episodes must be positive")
    report = index_tar(args.archive, args.max_episodes)
    _write_json_atomic(args.output, report)
    print(
        f"episodes={report['episode_count']} "
        f"precheck_valid={report['precheck_valid_count']} "
        f"stopped_early={report['stopped_early']}"
    )


if __name__ == "__main__":
    main()
