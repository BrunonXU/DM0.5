#!/usr/bin/env python3
"""从ZIP或tar.gz单遍提取已准入episode的训练必需成员。"""

from __future__ import annotations

import argparse
import json
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_RELATIVE_PATHS = (
    "meta_info.json",
    "record/aligned_joints.h5",
    "camera/head_color/head_color.h265",
    "camera/head_color/head_color.txt",
    "camera/hand_left_color/hand_left_color.h265",
    "camera/hand_left_color/hand_left_color.txt",
    "camera/hand_right_color/hand_right_color.h265",
    "camera/hand_right_color/hand_right_color.txt",
)
COPY_BUFFER_BYTES = 8 * 1024 * 1024


def _selected_roots(manifest: dict, archive_path: Path) -> dict[str, str]:
    resolved = str(archive_path.resolve())
    return {
        item["archive_member_root"]: item["episode"]
        for item in manifest["selected"]
        if str(Path(item["archive"]).resolve()) == resolved
    }


def _wanted_members(roots: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {
        f"{root}/{relative}": (episode, relative)
        for root, episode in roots.items()
        for relative in REQUIRED_RELATIVE_PATHS
    }


def _copy_stream(source, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        shutil.copyfileobj(source, output, COPY_BUFFER_BYTES)


def _finalize(
    roots: dict[str, str],
    output_root: Path,
    partial_root: Path,
) -> tuple[int, list[dict]]:
    completed = 0
    failures = []
    for episode in sorted(roots.values()):
        final_dir = output_root / episode
        if final_dir.exists():
            completed += 1
            continue
        partial_dir = partial_root / episode
        missing = [
            relative
            for relative in REQUIRED_RELATIVE_PATHS
            if not (partial_dir / relative).is_file()
        ]
        if missing:
            failures.append({"episode": episode, "missing": missing})
            continue
        partial_dir.replace(final_dir)
        completed += 1
    return completed, failures


def extract_required(
    manifest_path: Path,
    archive_path: Path,
    output_root: Path,
) -> dict:
    manifest = json.loads(manifest_path.read_text())
    roots = _selected_roots(manifest, archive_path)
    wanted = _wanted_members(roots)
    output_root.mkdir(parents=True, exist_ok=True)
    partial_root = output_root / ".partial"
    partial_root.mkdir(exist_ok=True)
    already_complete = {
        episode for episode in roots.values() if (output_root / episode).is_dir()
    }
    extracted_files = 0
    extracted_bytes = 0

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for member_name, (episode, relative) in wanted.items():
                if episode in already_complete:
                    continue
                with archive.open(member_name) as source:
                    target = partial_root / episode / relative
                    _copy_stream(source, target)
                    extracted_files += 1
                    extracted_bytes += archive.getinfo(member_name).file_size
    else:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                normalized = str(PurePosixPath(member.name.lstrip("./")))
                match = wanted.get(normalized)
                if match is None or not member.isfile():
                    continue
                episode, relative = match
                if episode in already_complete:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                with source:
                    _copy_stream(source, partial_root / episode / relative)
                extracted_files += 1
                extracted_bytes += member.size

    completed, failures = _finalize(roots, output_root, partial_root)
    return {
        "archive": str(archive_path.resolve()),
        "selected_episode_count": len(roots),
        "already_complete_count": len(already_complete),
        "completed_episode_count": completed,
        "extracted_file_count": extracted_files,
        "extracted_bytes": extracted_bytes,
        "failure_count": len(failures),
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = extract_required(args.manifest, args.archive, args.output_root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_name(f".{args.report.name}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.report)
    print(
        f"episodes={result['completed_episode_count']}/"
        f"{result['selected_episode_count']} "
        f"files={result['extracted_file_count']} "
        f"bytes={result['extracted_bytes']} "
        f"failures={result['failure_count']}"
    )
    if result["failure_count"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
