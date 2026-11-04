#!/usr/bin/env python3
"""Derive a new EEF version from immutable audited windows; no temporal re-pairing."""
import argparse
import json
from pathlib import Path
import shutil
import sys

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.eef import encode_actions, solver
from f1_toys.processing import sha256
from f1_toys.native.kinematics import LOW_DEG, HIGH_DEG


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    original = json.loads((args.source / "manifest.json").read_text())
    for name, digest in original["files_sha256"].items():
        if sha256(args.source / name) != digest:
            raise ValueError(f"Source hash mismatch: {name}")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(x) for x in (args.source / "samples.jsonl").read_text().splitlines()]
    with np.load(args.source / "arrays.npz") as data:
        states, joints = data["states"], data["actions"]
    model = solver().model
    # Cache repeated right FK poses across overlapping action windows.
    cache = {}
    fk = model.fk
    def cached_fk(arm, q):
        key = (arm, tuple(np.asarray(q, float)))
        if key not in cache:
            cache[key] = fk(arm, q)
        return cache[key]
    model.fk = cached_fk
    actions = np.stack([encode_actions(model, state, action) for state, action in zip(states, joints)])
    unique = list({(row["episode_id"], row["observation_id"]): i for i, row in enumerate(rows)}.values())
    observed = states[unique].astype(float)
    low, high = observed.min(0), observed.max(0)
    low[:7], high[:7] = LOW_DEG, HIGH_DEG
    low[14:], high[14:] = 0, 100
    xyz = actions[..., :3].reshape(-1, 3).astype(float)
    stats = dict(state=dict(low=low.tolist(), high=high.tolist(),
                           near_constant_indices=np.flatnonzero(high-low < 1e-4).tolist(),
                           near_constant_span_floor_deg=1., unique_observations=len(unique)),
                 action=dict(mean=xyz.mean(0).tolist(), std=np.maximum(xyz.std(0), 1e-6).tolist(),
                             rotation="identity", gripper="(g-50)/50", scope="all final anchored training windows"))
    cfg = json.loads((args.source / "contract.json").read_text())
    cfg.update(schema="f1_toys_eef_contract_v2", active_action_indices=list(range(10)),
               train_image_augmentation=True, roi_basis="User-selected A after historical review 20261016",
               prompt_status="Single V1 policy experiment authorized by user 20261016",
               normalization="state physical/minmax; delta xyz mean/std; rotation6D identity; gripper fixed",
               action_semantics="inv(FK(current raw feedback)) @ FK(submitted future action); fixed anchor H50",
               kinematics_sha256=model.config_sha256, rotation6d="first_two_matrix_rows",
               augmentation=dict(head=["area95pct_square_p0.5", "rotate2deg_p0.5", "color_p0.5"], wrists=["color_p0.5"]))
    np.savez_compressed(args.output / "arrays.npz", states=states, actions=actions, submitted=joints)
    shutil.copyfile(args.source / "samples.jsonl", args.output / "samples.jsonl")
    shutil.copytree(args.source / "images", args.output / "images")
    for name, content in (("contract.json", cfg), ("norm_stats.json", stats)):
        (args.output / name).write_text(json.dumps(content, indent=2) + "\n")
    manifest = dict(original, schema="f1_toys_eef_dataset_v2", smoke_only=False, formal_training_ready=False,
                    user_authorization="User authorized learning one V1 episode and end-to-end train/inference on 20261016; no generalization claim",
                    parent_manifest_sha256=sha256(args.source / "manifest.json"),
                    windows=len(rows), unique_observations=len(unique), temporal_windows_unchanged=True,
                    files_sha256={str(f.relative_to(args.output)): sha256(f) for f in args.output.rglob("*") if f.is_file()})
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(windows=len(rows), unique_observations=len(unique), stats=stats)))


if __name__ == "__main__":
    main()
