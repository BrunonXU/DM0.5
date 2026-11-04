#!/usr/bin/env python3
"""Exercise the trained checkpoint on recorded inputs, without robot/network I/O."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.dataset import F1Dataset
from f1_toys.inference import Policy
from f1_toys.processing import ACTIVE, sha256


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--rlinf-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--indices", default="0,227,455,682,909")
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["F1_TOYS_TOKENIZER"] = str(args.bundle / "paligemma_tokenizer.model")
    dataset = F1Dataset(args.dataset, allow_smoke_only=True)
    for name in ("contract.json", "norm_stats.json"):
        if sha256(args.dataset / name) != sha256(args.bundle / name):
            raise ValueError(f"Dataset/bundle mismatch: {name}")
    start = time.perf_counter()
    policy = Policy(args.bundle, args.rlinf_root)
    torch.cuda.synchronize()
    load_seconds = time.perf_counter() - start
    indices = [int(i) for i in args.indices.split(",")]
    reports, predictions = [], []
    for index in indices:
        held = dataset.actions[index, 0].copy()  # Recorded valid submitted target, offline only.
        start = time.perf_counter()
        actions, obs = policy.predict(dataset.raw_images(index), dataset.states[index], held)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        train_obs = dataset[index][0]
        for key in obs:
            if isinstance(obs[key], dict):
                for camera in obs[key]:
                    np.testing.assert_array_equal(obs[key][camera], train_obs[key][camera])
            else:
                np.testing.assert_array_equal(obs[key], train_obs[key])
        left = [i for i in range(16) if i not in ACTIVE]
        np.testing.assert_array_equal(actions[:, left], np.broadcast_to(held[left], (50, len(left))))
        assert actions.shape == (50, 16) and np.isfinite(actions).all()
        for key, pixels in obs["image"].items():
            Image.fromarray(np.rint((pixels + 1) * 127.5).astype(np.uint8)).save(args.output / f"{index}-{key}.png")
        error = actions[:, list(ACTIVE)] - dataset.actions[index][:, list(ACTIVE)]
        reports.append(dict(index=index, observation_id=dataset.rows[index]["observation_id"], seconds=elapsed,
                            right_arm_mae_degrees=float(np.abs(error[:, :7]).mean()), right_gripper_mae=float(np.abs(error[:, 7]).mean()),
                            finite=True, left_equals_last_submitted=True, train_inference_inputs_equal=True))
        predictions.append(actions)
        print(json.dumps(reports[-1]), flush=True)
    np.savez_compressed(args.output / "predictions.npz", indices=indices, actions=np.stack(predictions), labels=dataset.actions[indices])
    report = dict(result="PASS", smoke_only=True, robot_published=False, learned_task_success_not_evaluated=True,
                  bundle_sha256=sha256(args.bundle / "bundle.json"), torch_version=torch.__version__,
                  gpu=torch.cuda.get_device_name(), load_seconds=load_seconds,
                  max_allocated_gib=torch.cuda.max_memory_allocated()/1024**3, samples=reports)
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
