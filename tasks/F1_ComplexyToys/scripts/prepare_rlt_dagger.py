#!/usr/bin/env python3
"""Extend the immutable five-rollout replay with audited human corrections.

Human observations are encoded by the same frozen B1250 and RL-token encoder.
The old behavior actions/features remain bit-identical; this has no robot IO.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.rlt_replay import digest
from f1_toys.rlt_human_replay import audit_human_raw, select_human_segment
from f1_toys.rlt_stage2 import check_source_split
from rlt_stage2_offline import extract_feature, seed_all, write_json


def prepare(args, cfg, run):
    from f1_toys.eef import encode_actions, normalize_actions, solver
    from f1_toys.inference import Policy
    from f1_toys.rlt import FrozenTokenReader

    old_root = Path(cfg["base_replay"])
    old_manifest = json.loads((old_root / "manifest.json").read_text())
    if digest(old_root / "manifest.json") != cfg["base_replay_manifest_sha256"]:
        raise ValueError("Base replay identity changed")
    old_recipe_path = Path(cfg["base_recipe"])
    if digest(old_recipe_path) != old_manifest["recipe_sha256"]:
        raise ValueError("Base recipe identity changed")
    old_recipe = json.loads(old_recipe_path.read_text())
    for key in ("fps", "gamma_per_step", "output_parameterization", "reference_mode"):
        if cfg[key] != old_recipe[key]:
            raise ValueError("Old/new replay contract differs: " + key)
    if cfg["model"] != old_recipe["model"]:
        raise ValueError("Old/new model/action chunk contracts differ")
    for name, expected in old_manifest["files_sha256"].items():
        if digest(old_root / name) != expected:
            raise ValueError("Base replay file changed: " + name)
    old_records = [json.loads(line) for line in (old_root / "transitions.jsonl").read_text().splitlines()]
    old_data = torch.load(old_root / "replay.pt", map_location="cpu", weights_only=True)
    for record in old_records:
        record["source_group"] = record["source_run"]
        record["data_source"] = "autonomous_B1250"
        record["intervene_flags"] = [False] * cfg["model"]["chunk_len"]

    records, reports, receipts, raw = [], [], {}, {}
    for segment in cfg["human_segments"]:
        name = segment["source_run"]
        root = Path(cfg["human_raw_root"]) / name
        review_path = Path(cfg["human_review_root"]) / (name + ".json")
        if digest(review_path) != segment["review_sha256"]:
            raise ValueError("Human review identity changed")
        review = json.loads(review_path.read_text())
        streams, frames, receipt = audit_human_raw(root, review_path)
        selected, report = select_human_segment(streams, frames, segment, cfg, review)
        if not selected or sum(r["terminated"] and sum(r["rewards"]) > 0 for r in selected) != 1:
            raise ValueError("Human source lacks one audited successful terminal: " + name)
        for record in selected:
            record["data_source"] = "human_correction"
            record["phase"] = next((p["stage"] for p in review["phase_ranges"]
                if p["start_ns"] <= record["decision_ns"] <= p["end_ns"]), "unspecified")
            grip = [a[15] for a in record["submitted16"]]
            if min(grip) < 50 <= max(grip):
                record["phase"] = "release_transition"
        raw[name] = (root, {f["observation_id"]: f for f in frames})
        receipts[name] = receipt
        records.extend(selected)
        reports.append(report)
        print("HUMAN_RAW_AUDITED", name, len(selected), flush=True)
    combined_records = old_records + records
    check_source_split(combined_records)
    group_splits = {}
    for record in combined_records:
        group_splits.setdefault(record["source_group"], set()).add(record["split"])
    if any(len(splits) != 1 for splits in group_splits.values()):
        raise ValueError("Collection-session leakage")
    if not any(r["split"] == "validation" for r in records):
        raise ValueError("A held-out human correction is required")

    policy = Policy(cfg["parent_bundle"], args.core)
    policy.model.requires_grad_(False)
    reader = FrozenTokenReader(cfg["encoder"], policy, args.core)
    if old_manifest["parent_bundle_sha256"] != policy.bundle_id:
        raise ValueError("Frozen B1250 identity changed")
    if old_manifest["encoder_manifest_sha256"] != digest(Path(cfg["encoder"]) / "manifest.json"):
        raise ValueError("Frozen token encoder identity changed")
    kinematics = solver().model
    if kinematics.config_sha256 != policy.contract["kinematics_sha256"]:
        raise ValueError("Kinematics identity changed")
    keys = sorted({(r["source_run"], r[field]) for r in records
                   for field in ("observation_id", "next_observation_id")})
    offset = len(old_data["z_rl"])
    lookup = {key: offset + i for i, key in enumerate(keys)}
    features = []
    started = time.monotonic()
    for i, (name, oid) in enumerate(keys):
        root, frames = raw[name]
        feature = extract_feature(policy, reader, frames[oid], root, cfg["seed"] + offset + i)
        if not all(torch.isfinite(v).all() for v in feature.values()):
            raise ValueError("Nonfinite frozen feature")
        features.append(feature)
        if i % 20 == 0 or i + 1 == len(keys):
            run.log({"cache/human_observations": i + 1, "cache/total": len(keys),
                     "cache/observations_per_second": (i + 1) / (time.monotonic() - started)}, step=i + 1)
            print("HUMAN_FEATURES", i + 1, len(keys), flush=True)
    human_actions = []
    for record in records:
        action = encode_actions(kinematics, record["state16"], np.asarray(record["submitted16"]))
        human_actions.append(normalize_actions(action, policy.stats["action"])[..., :10])
    new_data = {k: torch.stack([f[k] for f in features]) for k in features[0]}
    new_data.update(actions=torch.as_tensor(np.stack(human_actions)),
        current=torch.tensor([lookup[(r["source_run"], r["observation_id"])] for r in records]),
        next=torch.tensor([lookup[(r["source_run"], r["next_observation_id"])] for r in records]),
        rewards=torch.tensor([r["rewards"] for r in records]),
        terminated=torch.tensor([r["terminated"] for r in records]),
        truncated=torch.tensor([r["truncated"] for r in records]),
        discount=torch.tensor([r["discount"] for r in records]))
    combined = {k: torch.cat((old_data[k], new_data[k])) for k in old_data}
    combined["intervene_flags"] = torch.tensor([r["intervene_flags"] for r in combined_records], dtype=torch.bool)
    for key in old_data:
        if not torch.equal(combined[key][:len(old_data[key])], old_data[key]):
            raise ValueError("Old cache was changed while merging")
    assert all(p.grad is None and not p.requires_grad for p in policy.model.parameters())
    assert all(p.grad is None and not p.requires_grad for p in reader.encoder.parameters())
    torch.save(combined, args.output / "replay.pt")
    (args.output / "transitions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in combined_records))
    audit = {"human_segments": reports, "human_sources": receipts,
             "base_records": len(old_records), "human_records": len(records),
             "records": len(combined_records), "observations": len(combined["z_rl"]),
             "source_group_splits": {k: sorted(v) for k, v in group_splits.items()},
             "old_cache_bit_identical": True, "raw_modified": False, "robot_commands": 0}
    write_json(args.output / "audit.json", audit)
    manifest = {"schema": "f1_rlt_dagger_replay_v1", "recipe_sha256": digest(args.recipe),
        "parent_bundle_sha256": policy.bundle_id,
        "encoder_manifest_sha256": digest(Path(cfg["encoder"]) / "manifest.json"),
        "base_replay_manifest_sha256": digest(old_root / "manifest.json"),
        "action_norm_stats": policy.stats["action"],
        "gripper_encoding": {"physical_open": 0, "physical_closed": 100,
                             "normalized_open": -1, "normalized_closed": 1},
        "records": len(combined_records), "observations": len(combined["z_rl"]),
        "reference_mode": cfg["reference_mode"], "reference_is_original_executed_proposal": False,
        "files_sha256": {n: digest(args.output / n) for n in ("audit.json", "transitions.jsonl", "replay.pt")},
        "human_sources": receipts, "vla_frozen": True, "encoder_frozen": True,
        "deployment_ready": False, "robot_commands": 0,
        "validation_scope": cfg["validation_scope"]}
    write_json(args.output / "manifest.json", manifest)
    run.summary.update({"records": len(combined_records), "human_records": len(records),
                        "observations": len(combined["z_rl"]), "old_cache_bit_identical": True})


def main():
    import wandb
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("recipe", "core", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--run-id", required=True)
    args = p.parse_args()
    if os.environ.get("CUDA_VISIBLE_DEVICES") not in ("4", "5", "6", "7"):
        raise ValueError("Use an explicitly checked authorized training GPU4-7")
    args.output.mkdir(parents=True, exist_ok=False)
    cfg = json.loads(args.recipe.read_text())
    seed_all(cfg["seed"])
    torch.set_num_threads(4)
    run = wandb.init(project=cfg["wandb"]["project"], entity=cfg["wandb"]["entity"],
        group=cfg["wandb"]["group"], id=args.run_id, name=cfg["experiment"] + "_cache",
        job_type="feature-cache", tags=["F1", "RLT_Stage2", "frozen_B1250", "human_corrections", "offline"],
        dir=str(args.output), resume="never", mode=os.environ.get("WANDB_MODE", "online"),
        config={"recipe": cfg, "recipe_sha256": digest(args.recipe),
                "runner_sha256": digest(__file__),
                "code_manifest_sha256": digest(TASK.parents[1] / "SOURCE_MANIFEST.json")})
    write_json(args.output / "wandb.json", {"id": run.id, "url": run.url, "mode": run.settings.mode})
    try:
        prepare(args, cfg, run)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__ == "__main__":
    main()
