#!/usr/bin/env python3
"""Verify real task batches, tokenizer parity, and deterministic inference inputs."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.dataset import F1Dataset, collate
from f1_toys.processing import ACTIVE, restore_actions


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    dataset = F1Dataset(args.dataset, allow_smoke_only=True)
    from openpi.models.tokenizer import PaligemmaTokenizer
    reference = PaligemmaTokenizer(200)
    indices = sorted(set([0, len(dataset)//4, len(dataset)//2, 3*len(dataset)//4, len(dataset)-1]))
    rows = []
    for index in indices:
        obs, actions = dataset[index]
        direct = dataset.processor.observation(dataset.raw_images(index), dataset.states[index])
        for key in obs["image"]:
            np.testing.assert_array_equal(obs["image"][key], direct["image"][key])
        tokens, masks = reference.tokenize(dataset.contract["prompt"], obs["state"])
        np.testing.assert_array_equal(tokens, obs["tokenized_prompt"])
        np.testing.assert_array_equal(masks, obs["tokenized_prompt_mask"])
        batch, labels = collate([(obs, actions)])
        assert tuple(labels.shape) == (1, 50, 32)
        assert all(bool(value.all()) for value in batch.image_masks.values())
        held = dataset.actions[index, 0].copy()
        restored = restore_actions(actions, dataset.stats["action"], held)
        np.testing.assert_allclose(restored[:, list(ACTIVE)], dataset.actions[index, :, list(ACTIVE)].T, rtol=1e-5, atol=1e-5)
        rows.append(dict(index=index, observation_id=dataset.rows[index]["observation_id"], labels=list(labels.shape),
                         tokens=int(masks.sum()), image_shapes={k:list(v.shape) for k,v in batch.images.items()}))
    report = dict(result="PASS", samples=len(dataset), checked=rows, tokenizer_matches_official=True,
                  train_inference_inputs_equal=True, right_action_roundtrip=True, formal_training_ready=False)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
