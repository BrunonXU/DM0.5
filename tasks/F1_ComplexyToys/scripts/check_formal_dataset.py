#!/usr/bin/env python3
"""Read real train/validation batches, check EEF reversibility, save ROI contact sheets."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import torch
from PIL import Image, ImageDraw

TASK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TASK / "src"), str(TASK / "scripts")]
from f1_toys.dataset import F1Dataset
from f1_toys.inference import load_core, batched_observation
from f1_toys.eef import normalize_actions, physical_actions, relative_pose, solver
from f1_toys.processing import process_image, sha256
from prepare_formal import fit_statistics


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--rlinf-root", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    train, val = (F1Dataset(a.dataset / k) for k in ("train", "validation"))
    _, core = load_core(a.rlinf_root)
    assert train.stats == val.stats
    assert train.contract == val.contract
    assert fit_statistics(train.states, train.actions, train.rows,
                          train.action_time_mask if train.time_masked else None) == train.stats
    assert not ({r["episode_id"] for r in train.rows} & {r["episode_id"] for r in val.rows})
    model = solver().model
    previews = {camera: [] for camera in train.contract["cameras"]}
    report = {}
    for kind, ds in (("train", train), ("validation", val)):
        groups = {}
        for i, row in enumerate(ds.rows):
            groups.setdefault(row["episode_id"], []).append(i)
        with np.load(ds.root / "arrays.npz") as data:
            submitted = data["submitted"]
        max_pose_error = 0.
        checked = 0
        for episode, indices in groups.items():
            selected = set(np.linspace(0, len(indices)-1, 5, dtype=int).tolist())
            for predicate in (lambda i: ds.rows[i].get("contains_home"),
                              lambda i: ds.rows[i].get("supervision_phase") == "home",
                              lambda i: not ds.action_time_mask[i].all()):
                candidates = [j for j, i in enumerate(indices) if predicate(i)]
                if candidates:
                    selected.update((candidates[0], candidates[-1]))
            for j in sorted(selected):
                index = indices[j]
                checked += 1
                obs, normalized = ds[index][:2]
                valid = int(ds.action_time_mask[index].sum())
                batch = batched_observation(obs, core.Observation, "cpu")
                labels = torch.as_tensor(normalized[None])
                assert labels.shape == (1, 50, 32) and not labels[..., 10:].any()
                assert all(bool(x.all()) for x in batch.image_masks.values())
                restored = physical_actions(normalized, ds.stats["action"])
                np.testing.assert_allclose(restored[:valid], ds.actions[index, :valid], atol=1e-6, rtol=1e-5)
                if ds.time_masked:
                    assert not normalized[valid:].any()
                images = ds.raw_images(index)
                direct = ds.processor.observation(images, ds.states[index])
                for key in obs["image"]:
                    np.testing.assert_array_equal(obs["image"][key], direct["image"][key])
                for k in sorted({0, min(10, valid-1), min(20, valid-1), valid-1}):
                    actual = model.fk(1, ds.states[index, 7:14]) @ relative_pose(restored[k])
                    expected = model.fk(1, submitted[index, k, 7:14])
                    max_pose_error = max(max_pose_error, float(np.max(np.abs(actual - expected))))
                    np.testing.assert_allclose(actual, expected, atol=2e-6)
                for camera, image in images.items():
                    tile = Image.new("RGB", (224, 244), "white")
                    tile.paste(Image.fromarray(process_image(image, camera, ds.contract)), (0, 20))
                    ImageDraw.Draw(tile).text((2, 3), f"{episode[-8:]} {kind} {j}/{len(indices)}", fill="black")
                    previews[camera].append(tile)
        low, high = np.asarray(ds.stats["state"]["low"]), np.asarray(ds.stats["state"]["high"])
        outside = (ds.states < low) | (ds.states > high)
        report[kind] = dict(windows=len(ds), episodes=len(groups), checked_windows=checked,
            partial_windows=int((~ds.action_time_mask.all(1)).sum()),
            valid_action_steps=int(ds.action_time_mask.sum()),
            max_fk_matrix_roundtrip_error=max_pose_error,
            right_state_outside_fraction=float(outside[:, 7:14].mean()),
            maximum_physical_deviation_by_dimension=np.maximum(low-ds.states, ds.states-high).clip(min=0).max(0).tolist(),
            manifest_sha256=sha256(ds.root / "manifest.json"))
    for camera, tiles in previews.items():
        sheet = Image.new("RGB", (5*224, ((len(tiles)+4)//5)*244), "white")
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i%5)*224, (i//5)*244))
        sheet.save(a.output / f"{camera}.png")
    # Actual fresh training augmentation, with the same contract; validation stays deterministic.
    train.training = True
    np.random.seed(20261016)
    obs, labels = train[len(train)//2][:2]
    assert np.isfinite(labels).all() and all(np.isfinite(x).all() for x in obs["image"].values())
    assert all(x.shape == (224, 224, 3) for x in obs["image"].values())
    report.update(result="PASS", train_only_stats_recomputed=True, same_train_inference_processing=True,
                  fixed_validation_examples=len(json.loads((val.root / "eval_indices.json").read_text())))
    (a.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
