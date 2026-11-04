#!/usr/bin/env python3
"""Compare the RLT sidecar to unchanged RTC on saved observations; zero I/O."""
import argparse
import json
import os
from pathlib import Path
import sys

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.dataset import F1Dataset
from f1_toys.inference import Policy
from f1_toys.rlt import FrozenTokenReader
from f1_toys.rlt_reference import RLTReferenceAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("recipe", "core", "encoder", "output"):
        parser.add_argument("--"+key, required=True, type=Path)
    args = parser.parse_args()
    assert os.environ["CUDA_VISIBLE_DEVICES"] in ("4", "5", "6", "7")
    recipe = json.loads(args.recipe.read_text())
    ds = F1Dataset(Path(recipe["dataset"])/"validation")
    policy = Policy(recipe["parent_bundle"], args.core)
    policy.model.requires_grad_(False)
    reader = FrozenTokenReader(args.encoder, policy, args.core)
    adapter = RLTReferenceAdapter(reader)
    # First, middle and final observations: not a success-rate evaluation.
    indices = [0, len(ds)//2, len(ds)-1]
    records = []
    for index in indices:
        images, state = ds.raw_images(index), ds.states[index]
        context = dict(bootstrap=True, delay_steps=14, shift_steps=16, coverage_steps=32,
            committed_actions16=[], previous_absolute_eef10=[], plan_origin_ns=0, base_plan_id=0)
        for mode in ("bootstrap", "committed_prefix"):
            plain, raw, _ = policy.predict_rtc(images,state,state,context,seed=recipe["seed"])
            wrapped, candidate, _, features = adapter.predict_rtc(images,state,state,context,seed=recipe["seed"])
            np.testing.assert_array_equal(raw, candidate)
            assert plain["success"] == wrapped["success"]
            if plain["success"]:
                np.testing.assert_array_equal(plain["actions"], wrapped["actions"])
            assert features["prefix_forward_calls"] == 1 and not features["executed_transition"]
            assert features["proprio"].shape == (1,8) and features["ref_chunk"].shape == (1,50,10)
            assert torch.isfinite(features["z_rl"]).all()
            if mode == "committed_prefix" and wrapped["success"]:
                np.testing.assert_array_equal(wrapped["actions"][:14],context["committed_actions16"])
            records.append({"index":index,"mode":mode,"raw_exact":True,"ik_success":plain["success"],
                "commands_exact":bool(plain["success"]),"prefix_calls":1,"robot_commands":0})
            if not plain["success"]:
                break  # Do not fabricate a valid committed plan after an IK refusal.
            context = {**context,"bootstrap":False,"committed_actions16":plain["actions"][:14],
                       "previous_absolute_eef10":plain["absolute_eef10"],"base_plan_id":1}
    assert any(r["mode"] == "committed_prefix" for r in records)
    assert all(p.grad is None for p in policy.model.parameters())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({"result":"PASS","cases":records,"robot_commands":0,
        "online_actor":False,"ref_chunk_space":"normalized_relative_eef10",
        "limits":"Saved observations only; reward/executed-transition/online environment not implemented"},indent=2)+'\n')
    print(args.output.read_text())


if __name__ == "__main__":
    main()
