#!/usr/bin/env python3
"""Package audited multi-episode EEF windows with isolated train/validation statistics."""
import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
from PIL import Image

TASK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TASK / "src"), str(TASK / "DataPrehandle")]
from f1_toys.eef import encode_actions, solver
from f1_toys.native.kinematics import LOW_DEG, HIGH_DEG
from f1_toys.processing import load_contract, sha256
from build_training_index import audit


def fit_statistics(states, actions, rows, action_time_mask=None):
    unique = list({(r["episode_id"], r["observation_id"]): i for i, r in enumerate(rows)}.values())
    observed = states[unique].astype(float)
    low, high = observed.min(0), observed.max(0)
    low[:7], high[:7] = LOW_DEG, HIGH_DEG
    low[14:], high[14:] = 0, 100
    xyz = (actions[..., :3].reshape(-1, 3) if action_time_mask is None
           else actions[..., :3][action_time_mask]).astype(float)
    if not len(xyz):
        raise ValueError("No valid action labels for statistics")
    return dict(state=dict(low=low.tolist(), high=high.tolist(),
        near_constant_indices=np.flatnonzero(high-low < 1e-4).tolist(),
        near_constant_span_floor_deg=1., unique_observations=len(unique)),
        action=dict(mean=xyz.mean(0).tolist(), std=np.maximum(xyz.std(0), 1e-6).tolist(),
                    rotation="identity", gripper="(g-50)/50", scope="training split anchored windows only"),
        source_episodes=sorted({r["episode_id"] for r in rows}), scope="train_only")


def validation_indices(kind, count, begin=0):
    """Keep existing V1 linspace96; independently held-out short episodes use all."""
    if count < 1:
        raise ValueError('Episode has no valid training windows')
    if kind == 'train':
        return []
    if kind != 'validation':
        raise ValueError('Unknown split')
    return (begin + np.linspace(0, count-1, min(96, count), dtype=int)).tolist()


def validate_split(split, summary):
    actual = {r["episode"]: r["source_manifest_sha256"] for r in summary["episodes"]}
    train, val = split["train"], split["validation"]
    rows = train + val
    if not train or not val or len({r["episode"] for r in rows}) != len(rows):
        raise ValueError("Both splits must be nonempty with disjoint episode identities")
    if {r["episode"]: r["manifest_sha256"] for r in rows} != actual:
        raise ValueError("Split must exactly partition the audited collection/version")
    groups = [{r.get('source_group', r['episode']) for r in part} for part in (train, val)]
    if groups[0] & groups[1]:
        raise ValueError('Related source sessions/scenes cannot cross splits')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("raw-root", "index-root", "split", "contract", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    a = p.parse_args()
    cfg = load_contract(a.contract)
    if cfg["schema"] != "f1_toys_eef_contract_v2":
        raise ValueError("Use the verified EEF contract")
    summary = json.loads((a.index_root / "summary.json").read_text())
    split = json.loads(a.split.read_text())
    validate_split(split, summary)
    if (summary["config"]["fps"] != cfg["fps"] or summary["config"]["horizon"] != cfg["horizon"]
            or summary["config"]["required_cameras"] != cfg["cameras"]):
        raise ValueError("Temporal and model contracts disagree")
    partial = a.output.with_name(a.output.name + ".partial")
    if a.output.exists() or partial.exists() or a.raw_root.resolve() in a.output.resolve().parents:
        raise ValueError("Use a new output outside raw recordings")
    partial.mkdir(parents=True)
    cfg["prompt_status"] = cfg.get("prompt_status", "User retained original prompt and authorized V1 formal training 20261016")
    split = dict(split, status="frozen_for_authorized_training")
    for name, doc in (("split.json", split), ("time-index-summary.json", summary)):
        (partial / name).write_text(json.dumps(doc, indent=2) + "\n")
    shutil.copyfile(a.index_root / "user-confirmation.json", partial / "user-confirmation.json")
    if cfg.get("temporal_supervision"):
        review = a.index_root / "home-review.json"
        if sha256(review) != cfg["home_supervision"]["review_sha256"]:
            raise ValueError("Home review differs from contract")
        shutil.copyfile(review, partial / "home-review.json")
    model = solver().model
    fk, cache = model.fk, {}
    def cached_fk(arm, q):
        key = (arm, tuple(np.asarray(q, float)))
        if key not in cache:
            cache[key] = fk(arm, q)
        return cache[key]
    model.fk = cached_fk
    if cfg["kinematics_sha256"] != model.config_sha256:
        raise ValueError("Native FK changed")
    stats = None
    reports = {}
    evidence = {r["episode"]: r for r in summary["episodes"]}
    for kind in ("train", "validation"):
        dest = partial / kind
        dest.mkdir()
        rows, states, joints, actions, eval_indices, time_masks = [], [], [], [], [], []
        image_hashes = {}
        for source in split[kind]:
            episode = source["episode"]
            raw, index = a.raw_root / episode, a.index_root / "episodes" / episode
            checked = audit(raw)
            if not checked["valid"] or checked["manifest_sha256"] != source["manifest_sha256"]:
                raise ValueError("Raw audit failed or identity changed: " + episode)
            for name, digest in evidence[episode]["files_sha256"].items():
                if sha256(index / name) != digest:
                    raise ValueError("Index integrity failure: " + episode + "/" + name)
            begin = len(rows)
            with (index / "anchors.jsonl").open() as stream:
                for line in stream:
                    row = json.loads(line)
                    if row["episode_id"] != episode:
                        raise ValueError("Mixed episode index")
                    images = {}
                    for frame in row["frames"]:
                        camera = frame["camera"]
                        relative = f"images/{camera}/{frame['payload_sha256']}.jpg"
                        images[camera] = relative
                        if relative in image_hashes:
                            continue
                        src = (raw / "payload" / frame["path"]).resolve()
                        if (raw / "payload").resolve() not in src.parents:
                            raise ValueError("Frame escapes payload root")
                        with src.open("rb") as f:
                            f.seek(frame["payload_offset"])
                            data = f.read(frame["payload_bytes"])
                        if hashlib.sha256(data).hexdigest() != frame["payload_sha256"]:
                            raise ValueError("Frame hash mismatch")
                        with Image.open(io.BytesIO(data)) as im:
                            im.load()
                            expected = tuple(cfg["head_resolution"]) if camera == "head" else (640, 480)
                            if im.size != expected or im.mode != "RGB":
                                raise ValueError("Unexpected source image geometry/color")
                        path = dest / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                        image_hashes[relative] = frame["payload_sha256"]
                    state = np.asarray(row["state"], np.float32)
                    joint = np.asarray([c["action"] for c in row["action_window"]], np.float32)
                    valid = len(joint)
                    if not 1 <= valid <= cfg['horizon'] or row['action_is_pad'] != [False]*valid + [True]*(cfg['horizon']-valid):
                        raise ValueError('Invalid contiguous action prefix')
                    if valid < cfg['horizon'] and not cfg.get('temporal_supervision'):
                        raise ValueError('Partial windows require a versioned temporal contract')
                    mask = np.arange(cfg['horizon']) < valid
                    encoded = np.zeros((cfg['horizon'], 10), np.float32)
                    encoded[:valid] = encode_actions(model, state, joint)
                    padded = np.zeros((cfg['horizon'], 16), np.float32)
                    padded[:valid] = joint
                    states.append(state); joints.append(padded)
                    actions.append(encoded); time_masks.append(mask)
                    rows.append(dict(episode_id=episode, observation_id=row["observation_id"],
                        anchor_monotonic_ns=row["anchor_monotonic_ns"], images=images,
                        action_grid_ns=[c["grid_monotonic_ns"] for c in row["action_window"]],
                        action_control_sequences=[c["control_stream_sequence"] for c in row["action_window"]],
                        action_sources=[c['source'] for c in row['action_window']],
                        valid_action_steps=valid, supervision_phase=row.get('supervision_phase', 'human'),
                        contains_home=row.get('contains_home', False), truncation_reason=row.get('truncation_reason')))
            count = len(rows) - begin
            eval_indices.extend(validation_indices(kind, count, begin))
            print(json.dumps(dict(split=kind, episode=episode, windows=count)), flush=True)
        states, joints, actions = map(np.stack, (states, joints, actions))
        if not all(np.isfinite(v).all() for v in (states, joints, actions)):
            raise ValueError("Nonfinite physical states/labels")
        time_masks = np.stack(time_masks)
        if kind == "train":
            stats = fit_statistics(states, actions, rows, time_masks if cfg.get("temporal_supervision") else None)
        np.savez_compressed(dest / "arrays.npz", states=states, submitted=joints, actions=actions, action_time_mask=time_masks)
        (dest / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        for name, doc in (("contract.json", cfg), ("norm_stats.json", stats), ("eval_indices.json", eval_indices)):
            (dest / name).write_text(json.dumps(doc, indent=2) + "\n")
        files = {str(f.relative_to(dest)): image_hashes.get(str(f.relative_to(dest))) or sha256(f)
                 for f in dest.rglob("*") if f.is_file()}
        report = dict(schema="f1_toys_eef_dataset_v3" if cfg.get("temporal_supervision") else "f1_toys_eef_dataset_v2", smoke_only=False,
            formal_training_ready=False, data_validation="raw/index/image/finite labels passed; real reader preflight separate",
            user_authorization=cfg.get("supervision_authorization", "User authorized V1 formal training with unchanged prompt 20261016"),
            human_success_reviewed=True, split=kind, source_episodes=split[kind],
            samples=len(rows), windows=len(rows), image_files=len(image_hashes),
            valid_action_steps=int(time_masks.sum()), partial_windows=int((~time_masks.all(1)).sum()),
            home_anchor_windows=sum(r['supervision_phase']=='home' for r in rows),
            windows_containing_home=sum(r['contains_home'] for r in rows),
            unique_observations=len({(r["episode_id"], r["observation_id"]) for r in rows}),
            contract_sha256=sha256(dest / "contract.json"), norm_stats_sha256=sha256(dest / "norm_stats.json"),
            split_sha256=sha256(partial / "split.json"), files_sha256=files)
        report['evaluation'] = dict(policy='linspace_min96_distinct_per_episode_v1',
            samples=len(eval_indices),
            episodes=dict(Counter(rows[i]['episode_id'] for i in eval_indices)),
            phases=dict(Counter(rows[i]['supervision_phase'] for i in eval_indices)))
        (dest / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        reports[kind] = {k: v for k, v in report.items() if k != "files_sha256"}
    assert reports["train"]["norm_stats_sha256"] == reports["validation"]["norm_stats_sha256"]
    manifest = dict(schema="f1_toys_collection_v1", splits=reports,
        builder_sha256=sha256(Path(__file__)), prompt=cfg["prompt"],
        files_sha256={str(f.relative_to(partial)): sha256(f) for f in partial.rglob("*") if f.is_file()})
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(partial, a.output)
    print(json.dumps(dict(output=str(a.output), splits=reports)), flush=True)


if __name__ == "__main__":
    main()
