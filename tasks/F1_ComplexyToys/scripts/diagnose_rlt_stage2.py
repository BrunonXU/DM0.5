#!/usr/bin/env python3
"""Compare saved RLT critics and actions on identical replay states, on CPU.

This program never constructs an optimizer, loads the VLA, or contacts a robot.
Only actual behavior actions at true terminal transitions have known Q targets;
the outcomes of alternative candidate actions are counterfactual and unknown.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import torch


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n")


def tensor_digest(module):
    h = hashlib.sha256()
    for name, tensor in module.state_dict().items():
        h.update(name.encode())
        h.update(tensor.detach().cpu().contiguous().numpy().tobytes())
    return h.hexdigest()


def resources():
    return dict(host=socket.gethostname(), uid=os.getuid(), pid=os.getpid(),
                time_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                disk_free_bytes=shutil.disk_usage("/u01").free,
                gpu_processes=subprocess.check_output([
                    "nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                    "--format=csv,noheader,nounits"], text=True))


def scalar_stats(values):
    values = values.flatten().float()
    return dict(mean=values.mean().item(), min=values.min().item(),
                max=values.max().item(), p95=torch.quantile(values, .95).item())


def action_error(first, second, xyz_std):
    delta = first - second
    return {
        "normalized_mse": delta.square().mean().item(),
        "xyz_normalized_mae": delta[..., :3].abs().mean().item(),
        "rotation6d_mae": delta[..., 3:9].abs().mean().item(),
        "gripper_normalized_mae": delta[..., 9].abs().mean().item(),
        "xyz_distance_mm_mean": (delta[..., :3] * xyz_std).norm(dim=-1).mean().item() * 1000,
        "gripper_command_points_mae": delta[..., 9].abs().mean().item() * 50,
        "gripper_signed_delta_mean": delta[..., 9].mean().item(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False, parents=True)
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    sys.path.insert(0, str(args.task_root / "src"))
    from f1_toys.rlt_stage2 import make_policy, td_target

    root = args.run_root
    cfg = json.loads((root / "recipe.json").read_text())
    manifest = json.loads((root / "cache/manifest.json").read_text())
    assert sha(root / "recipe.json") == manifest["recipe_sha256"]
    for name, expected in manifest["files_sha256"].items():
        assert sha(root / "cache" / name) == expected, name
    records = [json.loads(row) for row in (root / "cache/transitions.jsonl").read_text().splitlines()]
    data = torch.load(root / "cache/replay.pt", map_location="cpu", weights_only=True)
    assert len(records) == len(data["current"]) == manifest["records"]
    assert data["terminated"].dtype == torch.bool
    assert not (data["terminated"] & data["truncated"]).any()
    chunk_len = cfg["model"]["chunk_len"]
    reward = (data["rewards"] * cfg["gamma_per_step"] ** torch.arange(chunk_len)).sum(-1)
    assert ((data["rewards"] == 0) | (data["rewards"] == 1)).all()
    assert torch.allclose(data["discount"], torch.full_like(data["discount"], cfg["gamma_per_step"] ** chunk_len))
    xyz_std = torch.tensor(json.loads((Path(cfg["parent_bundle"]) / "norm_stats.json").read_text())["action"]["std"])
    fields = ("z_rl", "proprio", "ref_chunk")
    obs = {k: data[k][data["current"]] for k in fields}
    nxt = {k: data[k][data["next"]] for k in fields}
    checkpoint_hashes = {step: sha(root / f"train/step-{step:06d}.pt") for step in (300, 800)}
    original_audit = json.loads((root / "audit/report.json").read_text())
    resources_before = resources()
    write_json(args.output / "resources-before.json", resources_before)

    import wandb
    spec = cfg["wandb"]
    run = wandb.init(project=spec["project"], entity=spec["entity"], group=spec["group"],
        job_type="offline-zero-update-diagnosis", id=args.run_id, resume="never",
        name="B1250_RLT_FiveRollouts_Critic_Action_Diagnosis", dir=str(args.output),
        mode=os.environ.get("WANDB_MODE", "online"),
        tags=["F1", "RLT_Stage2", "five_raw_recordings", "zero_update", "cpu", "critic_diagnosis"],
        config=dict(recipe_sha256=sha(root / "recipe.json"),
                    replay_manifest_sha256=sha(root / "cache/manifest.json"),
                    source_sha256=sha(__file__), checkpoint_sha256=checkpoint_hashes,
                    parameter_updates=0, cpu_threads=4, device="cpu"))
    write_json(args.output / "wandb.json", dict(id=run.id, url=run.url, mode=run.settings.mode))

    models, targets, original_parameters = {}, {}, {}
    actions = {"behavior": data["actions"].reshape(-1, chunk_len, 10),
               "reference": obs["ref_chunk"][:, :chunk_len]}
    next_actions = {"reference": nxt["ref_chunk"][:, :chunk_len]}
    with torch.inference_mode():
        for step in (300, 800):
            checkpoint = torch.load(root / f"train/step-{step:06d}.pt", map_location="cpu", weights_only=False)
            assert checkpoint["recipe_sha256"] == manifest["recipe_sha256"]
            assert checkpoint["replay_manifest_sha256"] == sha(root / "cache/manifest.json")
            model = make_policy(args.core, cfg["model"]).eval().requires_grad_(False)
            model.load_state_dict(checkpoint["model"], strict=True)
            target = copy.deepcopy(model.q_head).eval().requires_grad_(False)
            target.load_state_dict(checkpoint["target_critic"], strict=True)
            models[step], targets[step] = model, target
            original_parameters[step] = (tensor_digest(model), tensor_digest(target))
            actions[f"actor{step}"] = model.sac_forward(obs, deterministic=True)[0].reshape(-1, chunk_len, 10)
            next_actions[f"actor{step}"] = model.sac_forward(nxt, deterministic=True)[0].reshape(-1, chunk_len, 10)

        q_values, bootstrap_values, td_values = {}, {}, {}
        audit_checks = []
        for step, model in models.items():
            q_values[step] = {name: model.sac_q_forward(obs, action.flatten(1)) for name, action in actions.items()}
            bootstrap_values[step] = {
                name: targets[step](model._critic_state(nxt), action.flatten(1)).min(-1).values
                for name, action in next_actions.items()}
            td_values[step] = {
                name: td_target(reward, data["terminated"], data["discount"], value)
                for name, value in bootstrap_values[step].items()}
            for value in (*q_values[step].values(), *td_values[step].values()):
                assert torch.isfinite(value).all()
            previous = next(r for r in original_audit["checkpoints"] if r["step"] == step)
            assert checkpoint_hashes[step] == previous["checkpoint_sha256"]
            terminal_delta = max((q_values[step]["behavior"][r["index"]] - torch.tensor(r["q"])).abs().max().item()
                                 for r in previous["terminals"])
            ref_mse = (actions[f"actor{step}"] - actions["reference"]).square().mean().item()
            assert terminal_delta < 1e-6 and abs(ref_mse - previous["reference_mse"]) < 1e-7
            for value in td_values[step].values():
                assert torch.equal(value[data["terminated"]], reward[data["terminated"]])
            audit_checks.append(dict(checkpoint=step, previous_terminal_q_max_abs_error=terminal_delta,
                                     previous_reference_mse_abs_error=abs(ref_mse-previous["reference_mse"])))

        groups = {split: torch.tensor([i for i, r in enumerate(records) if r["split"] == split])
                  for split in ("train", "validation")}
        groups.update({name: torch.tensor([i for i, r in enumerate(records) if r["segment_id"] == name])
                       for name in sorted({r["segment_id"] for r in records})})
        summaries = {}
        for group, indices in groups.items():
            summary = dict(count=len(indices), action_comparisons={}, critics={})
            for first, second in itertools.combinations(actions, 2):
                summary["action_comparisons"][f"{first}_minus_{second}"] = action_error(
                    actions[first][indices], actions[second][indices], xyz_std)
            for step in models:
                qs = q_values[step]
                item = dict(actions={}, actor_advantages={}, bootstrap={})
                for name, q in qs.items():
                    selected = q[indices]
                    item["actions"][name] = dict(q1=scalar_stats(selected[:, 0]), q2=scalar_stats(selected[:, 1]),
                        min_q=scalar_stats(selected.min(-1).values), outside_0_1_count=int(((selected < 0) | (selected > 1)).sum()))
                for name in ("actor300", "actor800"):
                    item["actor_advantages"][name] = dict(
                        q1_minus_reference=scalar_stats((qs[name]-qs["reference"])[indices, 0]),
                        q1_minus_behavior=scalar_stats((qs[name]-qs["behavior"])[indices, 0]))
                for name in next_actions:
                    item["bootstrap"][name] = dict(
                        next_min_q=scalar_stats(bootstrap_values[step][name][indices]),
                        td_target=scalar_stats(td_values[step][name][indices]),
                        td_mse_behavior=(qs["behavior"][indices] - td_values[step][name][indices, None]).square().mean().item(),
                        td_target_minus_fixed_reference=scalar_stats((td_values[step][name]-td_values[step]["reference"])[indices]))
                summary["critics"][str(step)] = item
            summaries[group] = summary

        row_outputs, terminals = [], []
        for i, record in enumerate(records):
            row = dict(index=i, segment=record["segment_id"], split=record["split"],
                outcome=record["segment_outcome"], decision_ns=record["decision_ns"],
                terminated=record["terminated"], truncated=record["truncated"],
                discounted_chunk_reward=reward[i].item(),
                known_behavior_q_target=reward[i].item() if record["terminated"] else None,
                q={str(step): {name: q[i].tolist() for name, q in values.items()} for step, values in q_values.items()},
                td_targets={str(step): {name: value[i].item() for name, value in values.items()} for step, values in td_values.items()},
                action_differences={f"{first}_minus_{second}": action_error(actions[first][i:i+1], actions[second][i:i+1], xyz_std)
                                    for first, second in itertools.combinations(actions, 2)})
            row_outputs.append(row)
            if record["terminated"]:
                terminals.append(row)
        for step, model in models.items():
            assert (tensor_digest(model), tensor_digest(targets[step])) == original_parameters[step]
            assert all(p.grad is None and not p.requires_grad for p in model.parameters())
            assert sha(root / f"train/step-{step:06d}.pt") == checkpoint_hashes[step]

    (args.output / "per-transition.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in row_outputs))
    report = dict(schema="f1_rlt_same_state_critic_action_diagnosis_v1", source_run=str(root),
        checkpoint_sha256=checkpoint_hashes, replay_manifest_sha256=sha(root / "cache/manifest.json"),
        source_sha256=sha(__file__), records=len(records), groups=summaries, terminals=terminals,
        checks=dict(previous_audit_reproduction=audit_checks, terminal_bootstrap_zero=True,
                    parameters_unchanged=True, original_checkpoint_files_unchanged=True,
                    finite_predictions=True, cache_hashes_verified=True),
        parameter_updates=0, robot_commands=0, device="cpu", deployment_ready=False,
        wandb_url=run.url, limitations=[
            "Only one held-out failure recording; no independent successful rollout.",
            "Alternative action Q values are predictions, not observed counterfactual rewards.",
            "Known terminal targets apply only to the executed behavior action.",
            "Reference actions were recomputed without RTC; they are not the historical executed proposal.",
            "Deterministic actor actions are evaluated; training bootstrap after warmup sampled fixed Gaussian noise.",
            "Cross-checkpoint comparisons do not isolate which training update caused any change.",
            "XYZ distance is per-step command target distance, not tracking error or measured robot motion.",
            "No parameter updates, robot execution, or task-success evaluation occurred."])
    write_json(args.output / "report.json", report)
    table = wandb.Table(columns=["segment", "critic", "action", "q1", "q2", "actual_behavior_target"])
    for row in terminals:
        for step, candidates in row["q"].items():
            for candidate, values in candidates.items():
                table.add_data(row["segment"], step, candidate, *values,
                               row["known_behavior_q_target"] if candidate == "behavior" else None)
    run.log({"diagnosis/terminal_cross_scores": table, "diagnosis/records": len(records),
             "diagnosis/parameter_updates": 0})
    run.summary.update(dict(parameter_updates=0, robot_commands=0, records=len(records),
                            parameters_unchanged=True, deployment_ready=False))
    run.finish()
    write_json(args.output / "resources-after.json", resources())
    print(json.dumps(dict(report=str(args.output / "report.json"), wandb_url=run.url,
                          parameter_updates=0, records=len(records)), indent=2))


if __name__ == "__main__":
    main()
