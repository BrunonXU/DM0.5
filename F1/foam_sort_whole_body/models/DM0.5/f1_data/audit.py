"""Read-only source audit + derived statistics; never starts training or control."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .images import extract_images
from .reader import Episode, Normalizer, decode_eef, fit_statistics, require, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--pose-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-candidates", action="store_true")
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--images-per-episode", type=int, default=0, help="0 no extraction; -1 all valid starts; positive evenly spaced smoke starts")
    args = parser.parse_args()
    require(not args.output.exists(), "output exists; use a new derived audit directory")
    sources = sorted(args.cases.glob("*/composed/episode.npz"))
    require(bool(sources), "no composed episodes")
    episodes = [Episode(p, args.pose_cache / p.parents[1].name / "pose_cache.npz", allow_candidates=args.allow_candidates) for p in sources]
    stats = fit_statistics([ep for ep in episodes if ep.split == "train"])
    normalizer = Normalizer(stats)
    args.output.mkdir(parents=True)
    rows = []
    for ep in episodes:
        max_norm_error = max_pose_error = max_rotation_error = 0.
        clipped = np.zeros(19, dtype=np.int64)
        quantized_error = np.zeros(19, dtype=np.float64)
        for t in ep.starts:
            sample = ep.sample(t)
            normalized = normalizer(sample)
            center = np.array(stats["state"]["center"])
            scale = np.array(stats["state"]["scale"])
            unclipped = (sample["state"] - center) / scale
            known_state = sample.get('state_mask', np.ones(19, bool))
            clipped += (np.abs(unclipped) > 1) & known_state
            bins = np.floor((normalized["state"] + 1) * .5 * 255)
            dequantized = (bins / 255 * 2 - 1) * scale + center
            quantized_error = np.maximum(quantized_error, np.where(known_state, np.abs(dequantized - sample["state"]), 0))
            restored = normalizer.denormalize_action(normalized["action"])
            supervised = sample['action_mask'][:, :23]
            max_norm_error = max(max_norm_error, float(np.abs(restored - sample["action"][:, :23])[supervised].max()))
            decoded = decode_eef(sample["action"][:, :23], sample["anchor"])
            expected = ep.poses["command_pose"][t:t + ep.horizon]
            max_pose_error = max(max_pose_error, float(np.abs(decoded[..., :3, 3] - expected[..., :3, 3]).max()))
            max_rotation_error = max(max_rotation_error, float(np.abs(decoded[..., :3, :3] - expected[..., :3, :3]).max()))
            require(not sample["action_mask"][:, 23:].any() and (sample["action"][:, 23:] == 0).all(), "padding leaked")
            require(np.array_equal(known_state[16:19], sample['action_mask'][0, 20:23]), 'current/future base validity mismatch')
            require(np.array_equal(sample["state"][16:19], sample["action"][0, 20:23]), "current/future base t=0 mismatch")
        row = {"episode": ep.episode, "split": ep.split, "valid_starts": len(ep.starts),
               "source_chunk_starts": int(ep.data["chunk_start_candidate_valid"].sum()),
               "source_sha256": ep.source_sha256, "pose_cache_sha256": sha256(ep.pose_cache),
               "max_action_affine_roundtrip_abs": max_norm_error,
               "max_eef_position_roundtrip_abs_m": max_pose_error,
               "max_eef_rotation_matrix_roundtrip_abs": max_rotation_error,
               "state_clipped_fraction": (clipped / max(len(ep.starts), 1)).tolist(),
               "state_dequantization_max_abs_physical_units": quantized_error.tolist(),
               "training_approved": False}
        if args.images_per_episode:
            require(args.raw_root is not None, "--raw-root required for image extraction")
            starts = ep.starts if args.images_per_episode < 0 else ep.starts[np.unique(np.linspace(0, len(ep.starts)-1, min(len(ep.starts), args.images_per_episode), dtype=int))]
            manifest = extract_images(ep, args.raw_root, args.output / "images" / ep.episode.replace("/", "_"), starts)
            row["image_observations_checked"] = len(starts)
            row["unique_images_decoded"] = sum(len(entries) for entries in manifest["images"].values())
        rows.append(row)
    (args.output / "normalization.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    report = {"schema": "f1_reader_engineering_audit_v1", "training_approved": False,
              "scope": "numerical reader and optional original image decoding; not formal data or robot acceptance",
              "episodes": rows, "total_starts": sum(r["valid_starts"] for r in rows)}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
