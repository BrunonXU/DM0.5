#!/usr/bin/env python3
"""按冻结split并行转换已通过审计的episode。"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from g2_sort.data_tools.convert_episode import convert_episode


def _convert_one(
    episode_id: str,
    split: str,
    episode_root: Path,
    output_root: Path,
    fps: int,
) -> dict:
    try:
        output = convert_episode(
            episode_root / episode_id,
            output_root,
            split,
            fps,
            remux=True,
        )
        return {"episode": episode_id, "split": split, "output": str(output)}
    except Exception as exc:
        return {
            "episode": episode_id,
            "split": split,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("split_manifest", type=Path)
    parser.add_argument("episode_root", type=Path)
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    if args.workers <= 0:
        parser.error("--workers must be positive")

    split_manifest = json.loads(args.split_manifest.read_text())
    jobs = []
    for split in ("train", "validation"):
        for episode_id in split_manifest.get(split, []):
            audit_path = args.audit_dir / f"{episode_id}.json"
            audit = json.loads(audit_path.read_text())
            if not audit.get("valid"):
                raise ValueError(f"split includes invalid episode: {episode_id}")
            jobs.append((episode_id, split))

    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _convert_one,
                episode_id,
                split,
                args.episode_root,
                args.output_root,
                args.fps,
            ): episode_id
            for episode_id, split in jobs
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if completed % 25 == 0 or completed == len(futures):
                print(
                    f"converted={completed}/{len(futures)} "
                    f"latest={result['episode']} "
                    f"ok={'error' not in result}",
                    flush=True,
                )

    failures = sorted(
        (result for result in results if "error" in result),
        key=lambda item: item["episode"],
    )
    summary = {
        "requested_episode_count": len(jobs),
        "converted_episode_count": len(jobs) - len(failures),
        "failed_episode_count": len(failures),
        "failures": failures,
    }
    summary_path = args.output_root / "conversion_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = summary_path.with_name(f".{summary_path.name}.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(summary_path)
    print(
        f"converted={summary['converted_episode_count']} "
        f"failed={len(failures)} summary={summary_path}",
        flush=True,
    )
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
