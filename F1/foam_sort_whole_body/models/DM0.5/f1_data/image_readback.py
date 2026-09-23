"""Full original-image readback through F1Dataset, with independent time-index checks.

No model, training, ROS or source modification. Image transforms/tokenization
remain covered by the separate model_smoke; this audit reads every original RGB.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path

import numpy as np

from .images import ImageLoader
from .reader import CAMERAS, Episode, F1Dataset, Normalizer, require, sha256


def check_camera_selection(times, selected, original_times, original_valid):
    require(np.all(np.diff(original_times) >= 0), "camera log index is unsorted")
    # The source contract skips invalid camera timestamps before causal lookup.
    # Selecting the newest raw frame would reintroduce rejected clock samples.
    valid_ids = np.flatnonzero(original_valid)
    require(len(valid_ids) > 0, "no valid original camera timestamps")
    positions = np.searchsorted(original_times[valid_ids], times, side="right") - 1
    require(np.all(positions >= 0), "camera selection precedes valid source")
    expected = valid_ids[positions]
    require(np.array_equal(selected, expected), "camera selection is not latest causal valid source")
    require(np.all(selected >= 0), "camera selection precedes source")
    require(np.all(original_valid[selected]), "invalid original camera timestamp selected")
    return times - original_times[selected]


def audit_episode(ep, image_root, camera_root, normalizer):
    directory = image_root / ep.episode.replace("/", "_")
    manifest = json.loads((directory / "manifest.json").read_text())
    require(manifest["full_valid_starts"] is True, "partial image cache")
    require(manifest["checked_starts"] == list(map(int, ep.starts)), "cached starts differ from reader starts")
    source = Path(manifest["raw_path"])
    stat = source.stat()
    require(stat.st_size == ep.meta["source_size_bytes"] == manifest["raw_size_bytes"], "raw size changed")
    require(stat.st_mtime_ns == ep.meta["source_mtime_ns"] == manifest["raw_mtime_ns"], "raw fingerprint changed")
    camera_path = camera_root / (ep.episode + ".npz")
    require(sha256(camera_path) == ep.meta["camera_npz_sha256"], "camera index/source SHA mismatch")
    ages = []
    with np.load(camera_path, allow_pickle=False) as original:
        for i, name in enumerate(CAMERAS):
            selected = ep.data["camera_source_indices"][ep.starts, i]
            logs = original[name + "_log_ns"]
            headers = original[name + "_header_ns"]
            age = check_camera_selection(ep.data["time_ns"][ep.starts], selected, logs,
                                         original[name + "_timestamp_candidate_valid"])
            require(np.allclose(age / 1e9, ep.data["camera_log_age_s"][ep.starts, i],
                                atol=1e-6, rtol=0), "camera source age mismatch")
            entries = manifest["images"][name]
            require(set(map(int, entries)) == set(map(int, selected)), "cache index coverage mismatch")
            for index in np.unique(selected):
                entry = entries[str(index)]
                require(entry["log_time_ns"] == int(logs[index]), "cache/original log time mismatch")
                require(entry["header_time_ns"] == int(headers[index]), "cache/original header time mismatch")
            ages.append(age)

    loader = ImageLoader(image_root)
    def raw_loader(episode, indices):
        return {f"images_{i+1}": image for i, image in enumerate(loader.raw_images(episode, indices))}

    dataset = F1Dataset([ep], normalizer, raw_loader)
    dimensions = {name: set() for name in CAMERAS}
    for index, t in enumerate(ep.starts):
        sample = dataset[index]
        require(sample["state"].shape == (19,), "state19 shape changed")
        require(sample["action"].shape == (50, 32), "action H50/pad32 shape changed")
        require(sample["action_mask"].dtype == bool and not sample["action_mask"][:, 23:].any(),
                "padding supervision changed")
        require(np.isfinite(sample["state"]).all() and np.isfinite(sample["action"]).all(),
                "nonfinite normalized sample")
        for i, name in enumerate(CAMERAS):
            image = sample[f"images_{i+1}"]
            try:
                entry = manifest["images"][name][str(ep.data["camera_source_indices"][t, i])]
                require(image.mode == "RGB" and list(image.size) == entry["size"], "decoded image geometry mismatch")
                dimensions[name].add(tuple(image.size))
            finally:
                image.close()
    log_times = np.stack([ep.data["time_ns"][ep.starts] - age for age in ages], axis=1)
    return {"episode": ep.episode, "split": ep.split, "status": "passed", "starts_read": len(dataset),
            "image_reads": 3 * len(dataset), "unique_images": sum(len(v) for v in manifest["images"].values()),
            "source_sha256": ep.source_sha256, "manifest_sha256": sha256(directory / "manifest.json"),
            "camera_index_sha256": sha256(camera_path),
            "dimensions": {name: sorted(sizes) for name, sizes in dimensions.items()},
            "log_age_ms": {name: {"min": float(age.min()/1e6), "p99": float(np.percentile(age,99)/1e6),
                                  "max": float(age.max()/1e6)} for name, age in zip(CAMERAS, ages)},
            "max_three_camera_log_spread_ms": float(np.ptp(log_times, axis=1).max()/1e6),
            "timestamp_scope": "recording log causality, not exposure synchronization", "training_approved": False}


def audit_source(job):
    """Each worker owns its image handles; failures remain explicit rows."""
    source, pose_cache, image_root, camera_root, stats, allow_candidates = job
    try:
        ep = Episode(source, pose_cache / source.parents[1].name / "pose_cache.npz",
                     allow_candidates=allow_candidates)
        return audit_episode(ep, image_root, camera_root, Normalizer(stats))
    except Exception as exc:
        return {"source": str(source), "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "pose-cache", "image-root", "camera-root", "statistics", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-candidates", action="store_true")
    parser.add_argument("--workers", type=int, default=4,
                        help="Independent CPU readers; use 1 for serial reference")
    args = parser.parse_args()
    require(1 <= args.workers <= 8, "workers must be between 1 and 8")
    args.output.mkdir(parents=True, exist_ok=False)
    stats = json.loads(args.statistics.read_text())
    sources = sorted(args.cases.glob("*/composed/episode.npz"))
    jobs = [(p, args.pose_cache, args.image_root, args.camera_root, stats, args.allow_candidates)
            for p in sources]
    rows = []
    # map preserves deterministic report order while separate processes decode
    # disjoint source caches. Every existing validation remains inside each job.
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for row in executor.map(audit_source, jobs):
            rows.append(row)
            with (args.output / "episodes.jsonl").open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            progress = {"completed": len(rows), "total": len(sources),
                        "failed": sum(r['status'] != 'passed' for r in rows),
                        "starts_read": sum(r.get('starts_read', 0) for r in rows)}
            pending = args.output / "progress.json.tmp"
            pending.write_text(json.dumps(progress) + "\n")
            pending.replace(args.output / "progress.json")
            print(json.dumps(row, ensure_ascii=False), flush=True)
    require(bool(rows), "no sources")
    report = {"schema": "f1_full_image_readback_v1", "status": "passed" if all(r["status"] == "passed" for r in rows) else "failed",
              "training_approved": False, "model_loaded": False, "hardware_execution": False,
              "source_modified": False, "tokenization_or_augmentation_full_read": False,
              "statistics_sha256": sha256(args.statistics), "episodes": rows,
              "reader_workers": args.workers, "audit_script_sha256": sha256(__file__),
              "total_starts_read": sum(r.get("starts_read", 0) for r in rows),
              "total_image_reads": sum(r.get("image_reads", 0) for r in rows),
              "total_unique_images": sum(r.get("unique_images", 0) for r in rows)}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
