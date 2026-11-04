#!/usr/bin/env python3
"""Paired, offline checkpoint replay on exactly matched historical observations.

Uses explicit identical initial noise, deterministic serving preprocessing and
the unchanged bounded IK. This is a conditional comparison, not a new rollout.
No network client, ROS import, robot owner or command publication.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import torch

from f1_toys.inference import Policy, batched_observation
from f1_toys.eef import solver, physical_actions, relative_pose, decode_prefix
from f1_toys.native.kinematics import LOW_DEG, HIGH_DEG


def observation_hash(data):
    h = hashlib.sha256()
    def visit(value):
        if isinstance(value, dict):
            for key in sorted(value):
                h.update(key.encode()); visit(value[key])
        else:
            x = np.ascontiguousarray(value)
            h.update(str(x.dtype).encode()); h.update(str(x.shape).encode()); h.update(x.tobytes())
    visit(data)
    return h.hexdigest()


def margins(q):
    q = np.asarray(q)
    return dict(j2_lower_deg=float(q[1]-LOW_DEG[1]), j6_upper_deg=float(HIGH_DEG[5]-q[5]),
                wrist_deg=float(110.5-1.025*abs(q[5])-abs(q[6])))


@torch.inference_mode()
def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--bundles", type=Path, required=True)
    p.add_argument("--rlinf-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(x) for x in (a.inputs / "inputs.jsonl").read_text().splitlines()]
    for name in ("contract.json", "norm_stats.json", "model_config.json", "paligemma_tokenizer.model"):
        assert (a.bundles / "bundle-5000" / name).read_bytes() == (a.bundles / "bundle-14000" / name).read_bytes(), name
    policies = {step: Policy(a.bundles / f"bundle-{step}", a.rlinf_root) for step in (5000, 14000)}
    noises = {seed: torch.randn((1, 50, 32), generator=torch.Generator().manual_seed(seed))
              for seed in range(20261019, 20261024)}
    np.savez(a.output / "initial-noise.npz", **{str(k): v.numpy() for k, v in noises.items()})
    noise_hash = {k: hashlib.sha256(v.numpy().tobytes()).hexdigest() for k, v in noises.items()}
    ik = solver()
    proof = dict(models={str(k): v.bundle_id for k, v in policies.items()},
                 inputs_sha256=hashlib.sha256((a.inputs / "inputs.jsonl").read_bytes()).hexdigest(),
                 seeds=list(noises), noise_sha256=noise_hash, observations=len(rows),
                 steps=5, prediction_horizon=50, execute_steps=20, robot_commands=0,
                 same_preprocessing_stats_contract=True, same_explicit_noise=True,
                 historical_noise_available=False,
                 limitation="Inputs follow historical 14K trajectories; no counterfactual closed-loop claim.")
    (a.output / "manifest.json").write_text(json.dumps(proof, indent=2)+"\n")
    raw_predictions = {}
    repeat_checked = False
    with (a.output / "results.jsonl").open("x") as stream:
        for row_index, row in enumerate(rows):
            request = row["request"]
            replay = request["prediction_replay"]
            state, held = np.asarray(replay["state16"]), np.asarray(replay["last_submitted16"])
            images = {}
            for camera, name in row["images"].items():
                path = a.inputs / name
                assert hashlib.sha256(path.read_bytes()).hexdigest() == row["image_sha256"][camera]
                with Image.open(path) as im:
                    images[camera] = im.convert("RGB")
            observations, hashes = {}, {}
            for step, policy in policies.items():
                data = policy.processor.observation(images, state)
                hashes[step] = observation_hash(data)
                observations[step] = batched_observation(data, policy.observation_type, policy.device)
            assert hashes[5000] == hashes[14000], "Model inputs differ"
            anchor = ik.model.fk(1, state[7:14])
            for seed, cpu_noise in noises.items():
                noise = cpu_noise.to("cuda")
                for step, policy in policies.items():
                    started = time.perf_counter()
                    with torch.autocast("cuda", dtype=torch.bfloat16):
                        prediction = policy.model.sample_actions(observations[step], num_steps=5, noise=noise.clone())
                        if not repeat_checked:
                            repeat = policy.model.sample_actions(observations[step], num_steps=5, noise=noise.clone())
                            torch.testing.assert_close(prediction, repeat, rtol=0, atol=0)
                            repeat_checked = True
                    raw = prediction[0].float().cpu().numpy()
                    action = physical_actions(raw, policy.stats["action"])
                    decoded = decode_prefix(ik, action, state, held, execute_steps=20)
                    diagnostics = decoded["diagnostics"]
                    q = np.asarray([d["candidate_joints"] for d in diagnostics])
                    limits = [margins(x) for x in q]
                    targets = [anchor @ relative_pose(x) for x in action[:20]]
                    key = f"{row_index}_{step}_{seed}"
                    raw_predictions[key] = raw
                    result = dict(row_index=row_index, episode=row["episode"], session_id=request["session_id"],
                                  request_id=request["request_id"], observation_id=request["observation_id"],
                                  observation_monotonic_ns=request["observation_monotonic_ns"],
                                  original_success=request["success"], step=step, seed=seed,
                                  input_sha256=hashes[step], noise_sha256=noise_hash[seed],
                                  state_right_deg=state[7:14].tolist(), held_right_deg=held[7:14].tolist(),
                                  state_margins=margins(state[7:14]), success=decoded["success"],
                                  failure_step_1_based=None if decoded["success"] else len(diagnostics),
                                  candidate_right_deg=q.tolist(), candidate_margins=limits,
                                  min_candidate_margins={k: min(x[k] for x in limits) for k in limits[0]},
                                  first_candidate_margins=limits[0], last_diagnostic=diagnostics[-1],
                                  target_poses= [t.tolist() for t in targets],
                                  physical_actions10=action.tolist(),
                                  elapsed_ms=(time.perf_counter()-started)*1000)
                    stream.write(json.dumps(result)+"\n")
            stream.flush()
            print(json.dumps(dict(completed_observations=row_index+1, total=len(rows),
                                  session=request["session_id"], request=request["request_id"])), flush=True)
    np.savez_compressed(a.output / "raw-predictions.npz", **raw_predictions)
    proof.update(completed=True, deterministic_repeat_bitwise_passed=repeat_checked,
                 model_input_hashes_all_matched=True, records=len(rows)*len(noises)*len(policies))
    (a.output / "manifest.json").write_text(json.dumps(proof, indent=2)+"\n")


if __name__ == "__main__":
    main()
