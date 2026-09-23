#!/usr/bin/env python3
"""Bind a chassis phase review to frozen source identities; write candidates only.

No source selection, original masks, MCAP, or 19D action pack is modified. An
episode excluded from the current selection remains excluded. Empty phase
reviews deliberately produce all-unknown sidecars for pipeline verification.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile

import numpy as np

import base_relabel

TASK = Path(__file__).resolve().parents[1]
REGISTRIES = {
    "source_775": TASK / "results/demo_775_selection/candidates.jsonl",
    "masks_775": TASK / "results/demo_775_selection/mask_manifest.jsonl",
    "current_772": TASK / "results/demo_772_selection/candidates.jsonl",
}
PRODUCTS = {
    "candidate/base_relabel.npz", "candidate/report.json", "candidate/manifest.json",
    "repair_binding.json", "import_report.json", "review_input.json",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def info(path, data=None):
    path = Path(path)
    if data is None:
        data = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": digest(data), "size_bytes": len(data)}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise base_relabel.RelabelError(message)


def registry(path):
    content = Path(path).read_bytes()
    rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    episodes = [row["episode"] for row in rows]
    require(len(episodes) == len(set(episodes)), f"duplicate episode in {path}")
    return {row["episode"]: row for row in rows}, info(path, content)


def verified_product(path, expected_sha, label):
    content = Path(path).read_bytes()
    actual = info(path, content)
    require(actual["sha256"] == expected_sha, f"{label} SHA256 does not match its frozen manifest")
    return content, actual


def array_identity(value):
    value = np.ascontiguousarray(value)
    return {"shape": list(value.shape), "dtype": str(value.dtype), "sha256": digest(value.tobytes()),
            "true_elements": int(np.count_nonzero(value))}


def prepare_binding(review_json, registry_paths=None):
    """Read/verify input without writing. Registry injection is for isolated tests."""
    registry_paths = REGISTRIES if registry_paths is None else registry_paths
    require(set(registry_paths) == set(REGISTRIES), "registry mapping must identify source_775/masks_775/current_772")
    phase_bytes = Path(review_json).read_bytes()
    phase = json.loads(phase_bytes)
    _, origin, duration_ns = base_relabel.validate_phases(phase)
    catalogs, registry_identity = {}, {}
    for key, path in registry_paths.items():
        catalogs[key], registry_identity[key] = registry(path)
    episode = phase["episode"]
    require(episode in catalogs["source_775"], "episode is absent from the frozen 775 source registry")
    require(episode in catalogs["masks_775"], "episode has no frozen conservative parent mask")
    source, mask = catalogs["source_775"][episode], catalogs["masks_775"][episode]
    current = catalogs["current_772"].get(episode)
    require(origin == source["source_start_ns"], "source_origin_ns does not match the source recording")
    require(duration_ns == source["source_duration_ns"] and
            phase["source_duration_s"] == source["source_duration_ns"] / 1_000_000_000,
            "source_duration_s does not match the source recording")
    require(source["source_stop_ns_exclusive"] - source["source_start_ns"] == duration_ns,
            "source registry contains an inconsistent duration")
    require(mask["parent_aligned_npz_sha256"] == source["aligned_npz_sha256"],
            "conservative mask parent is not the selected original aligned pack")
    require(mask["parent_split"] == source["parent_split"], "mask/source split mismatch")
    if current is not None:
        for field in ("source_start_ns", "source_duration_ns", "source_relative_path", "raw_path", "parent_split",
                      "signal_npz_sha256", "camera_npz_sha256", "aligned_npz_sha256", "aligned_metadata_sha256"):
            require(current[field] == source[field], f"current candidate has a different source identity: {field}")
    provenance = phase.get("provenance", {})
    require(isinstance(provenance, dict), "phase provenance must be an object")
    for field in ("signal_npz_sha256", "camera_npz_sha256"):
        if field in provenance:
            require(provenance[field] == source[field], f"phase provenance {field} does not match the source")

    products, raw_bytes = {}, {}
    specifications = {
        "original_aligned_npz": (source["aligned_npz_path"], source["aligned_npz_sha256"]),
        "original_aligned_metadata": (Path(source["aligned_npz_path"]).with_suffix(".json"), source["aligned_metadata_sha256"]),
        "parent_masked_aligned_npz": (mask["aligned_npz_path"], mask["aligned_npz_sha256"]),
        "parent_masked_aligned_metadata": (Path(mask["aligned_npz_path"]).with_suffix(".json"), mask["aligned_metadata_sha256"]),
        "signal_npz": (source["signal_npz_path"], source["signal_npz_sha256"]),
        "camera_npz": (source["camera_npz_path"], source["camera_npz_sha256"]),
    }
    for label, (path, expected) in specifications.items():
        raw_bytes[label], products[label] = verified_product(path, expected, label)
    original_meta = json.loads(raw_bytes["original_aligned_metadata"])
    parent_meta = json.loads(raw_bytes["parent_masked_aligned_metadata"])
    for label, meta in (("original", original_meta), ("masked parent", parent_meta)):
        require(meta["episode"] == episode and meta["start_ns"] == origin and
                meta["end_ns"] + 1 == source["source_stop_ns_exclusive"] and
                meta["frames"] == source["frames"] and meta["split"] == source["parent_split"] and
                meta["source_relative_path"] == source["source_relative_path"] and
                meta["camera_npz_sha256"] == source["camera_npz_sha256"],
                f"{label} metadata identity/time/split differs from the source row")
    require(parent_meta["parent_aligned_npz_sha256"] == source["aligned_npz_sha256"] and
            parent_meta["parent_aligned_metadata_sha256"] == source["aligned_metadata_sha256"],
            "masked metadata does not bind the original aligned source")
    raw_stat = Path(source["raw_path"]).stat()
    raw_fingerprint = {"size_bytes": raw_stat.st_size, "mtime_ns": raw_stat.st_mtime_ns}
    require(raw_fingerprint == source["source_fingerprint"], "raw MCAP size/mtime changed from the frozen source")
    with np.load(io.BytesIO(raw_bytes["original_aligned_npz"]), allow_pickle=False) as original, \
            np.load(io.BytesIO(raw_bytes["parent_masked_aligned_npz"]), allow_pickle=False) as parent:
        require(np.array_equal(original["time_ns"], parent["time_ns"]), "masked parent changed the original time grid")
        require(len(parent["time_ns"]) == source["frames"], "parent frame count differs from source")
        require(parent["time_ns"].dtype == np.dtype("int64"), "parent grid must be int64")
        require(str(parent["source_relative_path"].item()) == source["source_relative_path"], "parent NPZ source path mismatch")
        require(parent["state"].shape == (source["frames"], 21) and parent["action"].shape == (source["frames"], 19),
                "parent pack is not the expected 21D state / 19D action contract")
        mask_fields = [key for key in parent.files if "valid" in key]
        require(all(parent[key].dtype == np.dtype("bool") for key in mask_fields), "parent validity arrays must be boolean")
        masks = {key: array_identity(parent[key]) for key in mask_fields}
        tail_index = mask["tail_mask_start_index"]
        require(type(tail_index) is int and 0 <= tail_index <= source["frames"], "invalid tail mask start index")
        require(parent_meta["tail_mask_start_index"] == tail_index, "tail boundary differs between manifest and metadata")
        require(not parent["action_candidate_valid"][tail_index:].any(), "parent tail mask is not all-19D invalid")
        require(not parent["joint_frame_candidate_valid"][tail_index:].any(), "parent tail joint-valid mask was relaxed")
        grid = parent["time_ns"].copy()
    binding = {
        "schema": "f1_base_repair_binding_v1", "episode": episode,
        "review_input": info(review_json, phase_bytes), "registry_identity": registry_identity,
        "source_origin_ns": str(origin), "source_duration_ns": str(duration_ns),
        "source_relative_path": source["source_relative_path"], "raw_mcap": {
            "path": source["raw_path"], "fingerprint": raw_fingerprint,
            "verification": "frozen size/mtime plus verified derived SHA identities; raw SHA was not recomputed here"},
        "parent_split": source["parent_split"], "source_products": products,
        "source_eligibility": {
            "in_frozen_775_source_registry": True, "in_current_candidate": current is not None,
            "current_selection_version": "demo_772_v4",
            "status": "candidate_pending_acceptance" if current is not None else "excluded_diagnostic_source_not_restored",
            "selection_membership_changed": False,
        },
        "parent_masks": {"manifest_entry": mask, "array_identities": masks,
                         "tail_policy": parent_meta["tail_mask_policy"], "tail_mask_start_index": tail_index,
                         "tail_mask_start_s": mask["tail_mask_start_s"],
                         "policy": "Preserve these restrictions; this candidate does not relax any existing mask."},
        "limitations": [
            "Phase timing must be independently reviewed; an empty request is all unknown.",
            "Displacement/profile provenance remains weak; physical execution limits are not evaluated.",
            "HAL reconstruction is paused; held-target terminal arm supervision remains unresolved.",
            "No full 19D aligned pack, new chunk mask, training-reader acceptance or robot replay is produced.",
            "Excluded diagnostic sources are not automatically restored by chassis review.",
        ],
    }
    return phase_bytes, phase, binding, grid


def cached(output, identity):
    if not output.exists():
        return None
    require(output.is_dir() and not output.is_symlink(), "output must be a dedicated real directory")
    if not any(output.iterdir()):
        return None
    try:
        manifest = json.loads((output / "manifest.json").read_text())
        require(manifest["schema"] == "f1_base_repair_import_manifest_v1" and manifest["status"] == "complete" and
                manifest["identity"] == identity and manifest["identity_sha256"] == digest(canonical(identity)),
                "repair inputs/code/source identities changed; use a new output version")
        require(set(manifest["artifacts"]) == PRODUCTS, "incomplete repair artifacts; use a new output version")
        for name, expected in manifest["artifacts"].items():
            path = output / name
            require(not path.is_symlink(), "repair artifacts must not be symlinks")
            content = path.read_bytes()
            require(expected == {"sha256": digest(content), "size_bytes": len(content)},
                    f"repair artifact {name} changed; use a new output version")
    except (KeyError, OSError, ValueError) as exc:
        if isinstance(exc, base_relabel.RelabelError):
            raise
        raise base_relabel.RelabelError("existing repair output is incomplete; use a new output version") from exc
    report = json.loads((output / "import_report.json").read_text())
    return {"status": "reused", "output": str(output), "identity_sha256": digest(canonical(identity)),
            "source_eligibility": report["source_eligibility"], "candidate_counts": report["candidate_counts"]}


def run(review_json, output, registry_paths=None):
    review_json, output = Path(review_json), Path(output)
    require(not output.is_symlink(), "output must not be a symlink")
    output = output.absolute()
    phase_bytes, phase, binding, grid = prepare_binding(review_json, registry_paths)
    identity = {"review": binding["review_input"], "registry_identity": binding["registry_identity"],
                "binding_sha256": digest(canonical(binding)), "import_script": info(Path(__file__)),
                "relabel_script": info(Path(base_relabel.__file__))}
    reused = cached(output, identity)
    if reused:
        return reused
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        parent_path = binding["source_products"]["parent_masked_aligned_npz"]["path"]
        base_relabel.run(review_json, Path(parent_path), stage / "candidate")
        candidate_report = json.loads((stage / "candidate/report.json").read_text())
        require(candidate_report["identity"]["phase_json"]["sha256"] == binding["review_input"]["sha256"],
                "review changed during generation; retry from a stable input")
        require(candidate_report["identity"]["time_npz"]["sha256"] == binding["source_products"]["parent_masked_aligned_npz"]["sha256"],
                "parent NPZ changed during generation")
        with np.load(stage / "candidate/base_relabel.npz", allow_pickle=False) as candidate:
            require(np.array_equal(candidate["time_ns"], grid), "candidate does not preserve parent timestamps")
            unknown = ~candidate["base_candidate_present"]
            require(np.isnan(candidate["base_candidate_v"][unknown]).all(), "unknown candidates became numeric labels")
            require(not candidate["base_timing_reviewed"][unknown].any(), "unknown frames incorrectly claim reviewed timing")
            if not phase["segments"]:
                require(unknown.all() and (candidate["segment_index"] == -1).all(), "empty review imported old base labels")
        # Re-check read-only parents before publication; copying old values into
        # the candidate is not permitted and no parent file has been rewritten.
        for label, product in binding["source_products"].items():
            verified_product(product["path"], product["sha256"], label)
        require(Path(review_json).read_bytes() == phase_bytes, "review file changed during import")
        report = {
            "schema": "f1_base_phase_import_report_v1", "episode": phase["episode"],
            "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending_human_review_and_dataset_acceptance",
            "source_eligibility": binding["source_eligibility"], "candidate_counts": candidate_report["counts"],
            "phase_segments_supplied": len(phase["segments"]), "parent_time_ns_exactly_preserved": True,
            "unknown_is_nan_verified": True, "original_parents_unchanged_verified": True,
            "original_selection_and_masks_modified": False, "full_19d_pack_generated": False,
            "repair_completion": "not_claimed", "candidate_unresolved_reasons": candidate_report["unresolved_reasons"],
            "next_required": "Review explicit physical phase intervals/directions/distances, then use a new immutable candidate version.",
        }
        (stage / "review_input.json").write_bytes(phase_bytes)
        write_json(stage / "repair_binding.json", binding)
        write_json(stage / "import_report.json", report)
        artifacts = {}
        for name in sorted(PRODUCTS):
            product = info(stage / name)
            artifacts[name] = {"sha256": product["sha256"], "size_bytes": product["size_bytes"]}
        write_json(stage / "manifest.json", {"schema": "f1_base_repair_import_manifest_v1", "status": "complete",
                                            "identity": identity, "identity_sha256": digest(canonical(identity)),
                                            "artifacts": artifacts})
        try:
            os.rename(stage, output)
        except OSError:
            reused = cached(output, identity)
            if reused:
                return reused
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"status": "created", "output": str(output), "identity_sha256": digest(canonical(identity)),
            "source_eligibility": report["source_eligibility"], "candidate_counts": report["candidate_counts"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New dedicated repair candidate directory")
    args = parser.parse_args()
    try:
        result = run(args.review_json, args.output)
    except (base_relabel.RelabelError, OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, f"apply_base_phase_review: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
