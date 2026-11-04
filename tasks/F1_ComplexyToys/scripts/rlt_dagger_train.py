#!/usr/bin/env python3
"""Bounded offline RLT heads with verified expert BC and fixed terminal checks.

BC and Q are separate commands/runs. This program has no robot transport and
never promotes an offline checkpoint to deployment readiness.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import time

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.rlt_replay import digest
from f1_toys.rlt_stage2 import (actor_loss, actor_parameters, check_source_split,
                               make_policy, parameter_counts, td_target, update_target)
from rlt_stage2_offline import get_obs, seed_all, write_json

SCHEMA = "f1_rlt_dagger_training_v1"
EPSILON = 1e-12


def validate_replay(data, records, cfg):
    """Require real expert validation and keep sessions, not windows, apart."""
    check_source_split(records)
    sessions = {}
    for record in records:
        group = record.get("source_group", record["source_run"])
        if group in sessions and sessions[group] != record["split"]:
            raise ValueError("Same source_group appears in training and validation")
        sessions[group] = record["split"]
    count, chunk = len(records), cfg["model"]["chunk_len"]
    if data["actions"].shape != (count, chunk, cfg["model"]["action_dim"]):
        raise ValueError("Replay actions do not match the configured F1 chunk")
    flags = data.get("intervene_flags")
    if flags is None or flags.shape != (count, chunk) or flags.dtype != torch.bool:
        raise ValueError("Replay requires bool intervene_flags [transition, chunk]")
    for split in ("train", "validation"):
        selected = torch.tensor([r["split"] == split for r in records], device=flags.device)
        if not flags[selected].any():
            raise ValueError("Independent expert actions are required in " + split)
    for name in ("actions", "z_rl", "proprio", "ref_chunk", "rewards", "discount"):
        if not torch.isfinite(data[name]).all():
            raise ValueError("Nonfinite replay tensor: " + name)
    if data["rewards"].shape != (count, chunk):
        raise ValueError("Rewards must retain their exact within-chunk positions")
    if (data["rewards"] < 0).any() or (data["rewards"].sum(-1) > 1).any():
        raise ValueError("This evaluator requires the frozen one-time binary reward contract")
    if ((data["discount"] < 0) | (data["discount"] > 1)).any():
        raise ValueError("Discount must be in [0, 1]")


def normalization(cfg, manifest):
    stats = manifest.get("action_norm_stats", cfg.get("action_norm_stats"))
    if stats is None:
        path = manifest.get("action_norm_stats_path", cfg.get("action_norm_stats_path"))
        if path is None:
            raise ValueError("Cache manifest or recipe must bind action_norm_stats")
        stats = json.loads(Path(path).read_text())
    if "action" in stats:
        stats = stats["action"]
    mean, std = np.asarray(stats["mean"]), np.asarray(stats["std"])
    if mean.shape != (3,) or std.shape != (3,) or not np.isfinite(mean).all() or not np.isfinite(std).all() or (std <= 0).any():
        raise ValueError("Expected finite, positive xyz action normalization")
    gripper = manifest.get("gripper_encoding", cfg.get("gripper_encoding"))
    if gripper is None:
        raise ValueError("Gripper encoding must be explicit in cache manifest or recipe")
    expected = {"physical_open": 0, "physical_closed": 100,
                "normalized_open": -1, "normalized_closed": 1}
    if any(gripper.get(key) != value for key, value in expected.items()):
        raise ValueError("Unsupported gripper encoding; do not guess its open/closed meaning")
    return stats, gripper


def discounted_rewards(data, cfg):
    values = data["rewards"]
    powers = cfg["gamma_per_step"] ** torch.arange(values.shape[-1], device=values.device)
    return (values * powers).sum(-1, keepdim=True)


def action_metrics(prediction, expert, reference, flags, std, gripper):
    """All expert quantities use genuine intervention labels, never references."""
    error = prediction - expert
    selected = error[flags]
    metrics = {"expert_steps": int(flags.sum()),
               "reference_mse": float((prediction - reference).square().mean())}
    if selected.numel():
        metrics.update(expert_mse=float(selected.square().mean()),
                       expert_xyz_mae=float(selected[:, :3].abs().mean()),
                       expert_xyz_mm_mae=float((selected[:, :3] * std).abs().mean() * 1000),
                       expert_rotation6d_mae=float(selected[:, 3:9].abs().mean()),
                       expert_gripper_normalized_mae=float(selected[:, 9].abs().mean()))
        threshold = (gripper["normalized_open"] + gripper["normalized_closed"]) / 2
        holding = flags & (expert[..., 9] >= threshold)
        opening = flags & (expert[..., 9] < threshold)
        false_open = holding & (prediction[..., 9] < threshold)
        missed_open = opening & (prediction[..., 9] >= threshold)
        scale = ((gripper["physical_closed"] - gripper["physical_open"]) /
                 (gripper["normalized_closed"] - gripper["normalized_open"]))
        metrics.update(expert_gripper_physical_mae=float(selected[:, 9].abs().mean() * scale),
                       expert_hold_steps=int(holding.sum()),
                       expert_false_open_steps=int(false_open.sum()),
                       expert_false_open_rate=float(false_open.sum() / holding.sum()) if holding.any() else None,
                       expert_open_steps=int(opening.sum()),
                       expert_missed_open_steps=int(missed_open.sum()),
                       expert_missed_open_rate=float(missed_open.sum() / opening.sum()) if opening.any() else None)
    return metrics


def bound_metrics(q):
    violation = torch.maximum((-q).clamp(min=0), (q - 1).clamp(min=0))
    return {"q_min": float(q.min()), "q_max": float(q.max()),
            "q_bound_violation_max": float(violation.max()),
            "q_bound_violation_fraction": float((violation > EPSILON).float().mean())}


@torch.no_grad()
def evaluate(model, target, data, records, cfg, stats, gripper, phase):
    training = model.training
    model.eval()
    obs, nxt = get_obs(data, data["current"]), get_obs(data, data["next"])
    chunk, dim = cfg["model"]["chunk_len"], cfg["model"]["action_dim"]
    pi = model.sac_forward(obs, deterministic=True)[0].reshape(-1, chunk, dim)
    ref = model._get_ref_chunk(obs).reshape_as(pi)
    next_pi = (model._get_ref_chunk(nxt) if phase == "bc"
               else model.sac_forward(nxt, deterministic=True)[0])
    next_q = target(model._critic_state(nxt), next_pi).min(-1, keepdim=True).values
    reward = discounted_rewards(data, cfg)
    y = td_target(reward, data["terminated"][:, None], data["discount"][:, None], next_q)
    q_values = {"actual": model.sac_q_forward(obs, data["actions"]),
                "reference": model.sac_q_forward(obs, ref.flatten(1)),
                "actor": model.sac_q_forward(obs, pi.flatten(1))}
    std = torch.as_tensor(stats["std"], device=pi.device, dtype=pi.dtype)
    metrics, episodes, terminals, phases = {}, [], [], []
    for split in ("train", "validation"):
        ix = torch.tensor([i for i, row in enumerate(records) if row["split"] == split], device=pi.device)
        details = action_metrics(pi[ix], data["actions"][ix], ref[ix], data["intervene_flags"][ix], std, gripper)
        metrics.update({split + "/" + key: value for key, value in details.items() if value is not None})
        metrics[split + "/critic_td_mse"] = float((q_values["actual"][ix] - y[ix]).square().mean())
        for name, q in q_values.items():
            metrics[split + "/q_" + name + "_mean"] = float(q[ix].mean())
            for key, value in bound_metrics(q[ix]).items():
                metrics[split + "/" + name + "_" + key] = value
        episode_scores = []
        for source in sorted({records[i]["source_run"] for i in ix.tolist()}):
            rows = torch.tensor([i for i in ix.tolist() if records[i]["source_run"] == source], device=pi.device)
            row = {"split": split, "source_run": source,
                   **action_metrics(pi[rows], data["actions"][rows], ref[rows], data["intervene_flags"][rows], std, gripper)}
            episodes.append(row)
            if "expert_mse" in row:
                episode_scores.append(row["expert_mse"])
        metrics[split + "/expert_episode_macro_mse"] = float(np.mean(episode_scores))
        def phase_label(index):
            return str(records[index].get("phase", records[index].get("segment_id", "unspecified")))
        for label in sorted({phase_label(i) for i in ix.tolist()}):
            rows = torch.tensor([i for i in ix.tolist() if phase_label(i) == label], device=pi.device)
            phases.append({"split": split, "phase": label,
                           **action_metrics(pi[rows], data["actions"][rows], ref[rows], data["intervene_flags"][rows], std, gripper)})
        by_outcome = {"success": [], "failure": []}
        for i in ix.tolist():
            if not bool(data["terminated"][i]):
                continue
            expected = float(reward[i, 0])
            outcome = "success" if expected > 0 else "failure"
            error = (q_values["actual"][i] - expected).abs()
            by_outcome[outcome].append(float(error.mean()))
            terminals.append({"index": i, "split": split, "source_run": records[i]["source_run"],
                              "segment_id": records[i].get("segment_id"), "outcome": outcome,
                              "fixed_target": expected, "actual_q": q_values["actual"][i].tolist(),
                              "reference_q": q_values["reference"][i].tolist(),
                              "actor_q": q_values["actor"][i].tolist(), "actual_mae": float(error.mean())})
        outcome_errors = []
        for outcome, errors in by_outcome.items():
            metrics[split + "/terminal_" + outcome + "_count"] = len(errors)
            if errors:
                value = float(np.mean(errors))
                metrics[split + "/terminal_" + outcome + "_mae"] = value
                outcome_errors.append(value)
        if outcome_errors:
            metrics[split + "/terminal_macro_mae"] = float(np.mean(outcome_errors))
    all_q = torch.cat(list(q_values.values()))
    metrics.update({"all/" + key: value for key, value in bound_metrics(all_q).items()})
    if training:
        model.train()
    report = {"metrics": metrics, "episodes": episodes, "phases": phases, "terminals": terminals,
              "validation_role": "development_checkpoint_selection_not_fresh_test",
              "limitations": ["TD targets depend on learned values and are not ground-truth success labels.",
                              "Reference/actor Q at a logged terminal is counterfactual, not a fixed reward label.",
                              "Gripper rates classify expert command thresholds, not physical grasp success.",
                              "Independent success coverage is reported by terminal counts; it is never inferred."]}
    return metrics, report


def monitor_state():
    return {"best_expert": None, "best_step": 0, "nonimproving": 0, "q_bad_checks": 0}


def monitor_update(state, metrics, *, phase, step, cfg, baseline):
    """Budget/early-stop heuristics, distinct from a deployment acceptance test."""
    score = metrics["validation/expert_episode_macro_mse"]
    eligible = True
    reason = None
    if phase == "q":
        if "validation/terminal_macro_mae" not in metrics or "validation/terminal_macro_mae" not in baseline:
            raise ValueError("Q branch requires fixed independent terminal diagnostics")
        terminal_worse = metrics["validation/terminal_macro_mae"] > baseline["validation/terminal_macro_mae"] + EPSILON
        expert_worse = score > baseline["validation/expert_episode_macro_mse"] + EPSILON
        bound_worse = metrics["all/q_bound_violation_max"] > baseline["all/q_bound_violation_max"] + EPSILON
        bad = (terminal_worse and expert_worse) or bound_worse
        state["q_bad_checks"] = state["q_bad_checks"] + 1 if bad else 0
        eligible = not terminal_worse and not bound_worse
        if state["q_bad_checks"] >= 2:
            reason = "q_fixed_diagnostics_degraded_twice"
    improved = eligible and (state["best_expert"] is None or score < state["best_expert"] - EPSILON)
    if improved:
        state.update(best_expert=score, best_step=step, nonimproving=0)
    else:
        state["nonimproving"] += 1
    if phase == "bc" and step >= cfg["warmup_steps"] and state["nonimproving"] >= cfg["patience"]:
        reason = "expert_validation_patience"
    return improved, reason


def sample_indices(records, rng, cfg):
    groups = sorted({r["segment_id"] for r in records if r["split"] == "train"})
    grouped = {g: np.array([i for i, r in enumerate(records) if r["split"] == "train" and r["segment_id"] == g]) for g in groups}
    terminals = np.array([i for i, r in enumerate(records) if r["split"] == "train" and r["terminated"]])
    selected = []
    for _ in range(cfg["batch_size"]):
        if len(terminals) and rng.random() < cfg.get("terminal_sample_probability", 0.1):
            selected.append(int(rng.choice(terminals)))
        else:
            selected.append(int(rng.choice(grouped[rng.choice(groups)])))
    return selected


def checkpoint_identity(args, cfg):
    return {"recipe_sha256": digest(args.recipe), "replay_manifest_sha256": digest(args.cache / "manifest.json"),
            "runner_sha256": digest(__file__), "adapter_sha256": digest(TASK / "src/f1_toys/rlt_stage2.py"),
            "core_rlt_policy_sha256": digest(args.core / "rlinf/models/embodiment/mlp_policy/rlt_mlp_policy.py")}


def start_run(args, cfg, identity):
    import wandb
    spec = cfg["wandb"]
    run = wandb.init(project=spec["project"], entity=spec["entity"], id=args.run_id,
        name=cfg["experiment"] + "_" + args.phase + "_" + args.job, job_type=args.job,
        group="00_Engineering_Checks" if args.job == "preflight" else spec["group"],
        tags=["F1", "RLT_Stage2", "frozen_B1250", "expert_DAgger", "offline", args.phase, args.job],
        resume="must" if args.resume else "never", dir=str(args.output),
        mode=os.environ.get("WANDB_MODE", "online"), config={"recipe": cfg, "phase": args.phase, **identity})
    write_json(args.output / "wandb.json", {"id": run.id, "url": run.url, "mode": run.settings.mode})
    return run


def train(args, cfg, run, identity):
    manifest = json.loads((args.cache / "manifest.json").read_text())
    if manifest["recipe_sha256"] != identity["recipe_sha256"]:
        raise ValueError("Replay recipe identity mismatch")
    for name, expected in manifest["files_sha256"].items():
        if digest(args.cache / name) != expected:
            raise ValueError("Replay changed: " + name)
    stats, gripper = normalization(cfg, manifest)
    records = [json.loads(line) for line in (args.cache / "transitions.jsonl").read_text().splitlines()]
    data = {key: value.cuda() for key, value in torch.load(args.cache / "replay.pt", weights_only=True).items()}
    validate_replay(data, records, cfg)
    model = make_policy(args.core, cfg["model"]).cuda()
    target = copy.deepcopy(model.q_head).eval().requires_grad_(False)
    actor_optim = torch.optim.Adam(actor_parameters(model), lr=cfg["learning_rate"])
    critic_optim = torch.optim.Adam(model.q_head.parameters(), lr=cfg["learning_rate"])
    rng = np.random.default_rng(cfg["seed"])
    step, actor_updates, critic_updates, inherited_actor, inherited_critic = 0, 0, 0, 0, 0
    history, monitor, baseline = [], monitor_state(), None
    source_checkpoint = args.resume or args.init_checkpoint
    if source_checkpoint:
        saved = torch.load(source_checkpoint, map_location="cpu", weights_only=False)
        if saved["schema"] != SCHEMA or saved["identity"] != identity:
            raise ValueError("Checkpoint dataset/config/code identities changed")
        if args.resume and (saved["phase"] != args.phase or saved["run_id"] != args.run_id):
            raise ValueError("Resume must preserve phase and W&B run identity")
        if args.init_checkpoint and (args.phase != "q" or saved["phase"] != "bc"):
            raise ValueError("Only an explicitly selected BC checkpoint can initialize a Q branch")
        model.load_state_dict(saved["model"], strict=True)
        target.load_state_dict(saved["target_critic"], strict=True)
        actor_optim.load_state_dict(saved["actor_optimizer"])
        critic_optim.load_state_dict(saved["critic_optimizer"])
        rng.bit_generator.state = saved["numpy_rng"]
        torch.set_rng_state(saved["torch_rng"])
        torch.cuda.set_rng_state(saved["cuda_rng"])
        if args.resume:
            step, history, monitor, baseline = saved["step"], saved["history"], saved["monitor"], saved["baseline"]
            actor_updates, critic_updates = saved["actor_updates"], saved["critic_updates"]
            inherited_actor, inherited_critic = saved["inherited_actor_updates"], saved["inherited_critic_updates"]
        else:
            inherited_actor = saved["actor_updates"] + saved["inherited_actor_updates"]
            inherited_critic = saved["critic_updates"] + saved["inherited_critic_updates"]
    elif args.phase == "q":
        raise ValueError("Q is a separate reviewed branch: provide --init-checkpoint or --resume")
    counts = parameter_counts(model)
    write_json(args.output / "architecture.json", counts)
    run.summary.update(counts)
    cap = cfg["warmup_max_steps"] if args.phase == "bc" else cfg["q_max_steps"]
    limit = cap if args.steps is None else args.steps
    if not step < limit <= cap:
        raise ValueError("Requested updates must advance the checkpoint within the frozen phase budget")
    reward = discounted_rewards(data, cfg)
    started, resumed_at = time.monotonic(), step
    last_checkpoint = None

    def save(at):
        path = args.output / f"step-{at:06d}.pt"
        state = {"schema": SCHEMA, "phase": args.phase, "run_id": args.run_id, "identity": identity,
            "model": model.state_dict(), "target_critic": target.state_dict(),
            "actor_optimizer": actor_optim.state_dict(), "critic_optimizer": critic_optim.state_dict(),
            "step": at, "actor_updates": actor_updates, "critic_updates": critic_updates,
            "inherited_actor_updates": inherited_actor, "inherited_critic_updates": inherited_critic,
            "history": history, "monitor": monitor, "baseline": baseline,
            "numpy_rng": rng.bit_generator.state, "torch_rng": torch.get_rng_state(),
            "cuda_rng": torch.cuda.get_rng_state(), "deployment_ready": False}
        temporary = path.with_suffix(".tmp")
        torch.save(state, temporary)
        temporary.replace(path)
        return path

    if step == 0:
        metrics, report = evaluate(model, target, data, records, cfg, stats, gripper, args.phase)
        baseline = metrics.copy()
        monitor_update(monitor, metrics, phase=args.phase, step=0, cfg=cfg, baseline=baseline)
        history.append({"step": 0, **metrics})
        write_json(args.output / "eval-000000.json", report)
        last_checkpoint = save(0)
        shutil.copyfile(last_checkpoint, args.output / "best.pt")
        run.log(metrics, step=0)
    stop_reason = "phase_budget_exhausted"
    while step < limit:
        step += 1
        ix = torch.tensor(sample_indices(records, rng, cfg), device="cuda")
        obs, nxt = get_obs(data, data["current"][ix]), get_obs(data, data["next"][ix])
        with torch.no_grad():
            next_pi = model._get_ref_chunk(nxt) if args.phase == "bc" else model.sac_forward(nxt)[0]
            next_q = target(model._critic_state(nxt), next_pi).min(-1, keepdim=True).values
            y = td_target(reward[ix], data["terminated"][ix, None], data["discount"][ix, None], next_q)
        critic_optim.zero_grad(set_to_none=True)
        q = model.sac_q_forward(obs, data["actions"][ix])
        critic_loss = (q - y).square().mean()
        critic_loss.backward()
        critic_grad = torch.nn.utils.clip_grad_norm_(model.q_head.parameters(), cfg["clip_grad"])
        if not torch.isfinite(critic_loss) or not torch.isfinite(critic_grad):
            raise ValueError("Nonfinite critic update")
        critic_optim.step()
        critic_updates += 1
        actor_metrics = {}
        if args.phase == "bc" or step % cfg["actor_update_interval"] == 0:
            actor_optim.zero_grad(set_to_none=True)
            loss, actor_metrics = actor_loss(model, obs, q_weight=0.0 if args.phase == "bc" else cfg["q_weight"],
                bc_weight=cfg["bc_weight"], reference_dropout_prob=cfg["reference_dropout_prob"],
                human_action=data["actions"][ix], human_mask=data["intervene_flags"][ix])
            loss.backward()
            actor_grad = torch.nn.utils.clip_grad_norm_(actor_parameters(model), cfg["clip_grad"])
            if not torch.isfinite(loss) or not torch.isfinite(actor_grad):
                raise ValueError("Nonfinite actor update")
            actor_optim.step()
            actor_updates += 1
            actor_metrics.update({"actor/loss": loss.detach(), "actor/grad_norm": actor_grad.detach()})
        update_target(target, model.q_head, cfg["tau"])
        metrics = {"critic/loss": float(critic_loss), "critic/grad_norm": float(critic_grad),
            "train/learning_rate": cfg["learning_rate"], "train/q_weight": 0.0 if args.phase == "bc" else cfg["q_weight"],
            "train/actor_updates": actor_updates, "train/critic_updates": critic_updates,
            "train/total_actor_updates": actor_updates + inherited_actor,
            "train/total_critic_updates": critic_updates + inherited_critic,
            "train/updates_per_second": (step - resumed_at) / (time.monotonic() - started),
            "train/gpu_peak_mib": torch.cuda.max_memory_allocated() / 1024**2,
            **{key: float(value) for key, value in actor_metrics.items()}}
        reason = None
        if step % cfg["eval_interval"] == 0 or step == limit:
            values, report = evaluate(model, target, data, records, cfg, stats, gripper, args.phase)
            metrics.update(values)
            improved, reason = monitor_update(monitor, values, phase=args.phase, step=step, cfg=cfg, baseline=baseline)
            history.append({"step": step, **metrics})
            write_json(args.output / f"eval-{step:06d}.json", report)
            last_checkpoint = save(step)
            if improved:
                shutil.copyfile(last_checkpoint, args.output / "best.pt")
            metrics["checkpoint/step"] = step
            print(json.dumps({"step": step, "best_step": monitor["best_step"], "stop_reason": reason, **values}), flush=True)
        run.log(metrics, step=step)
        if reason:
            stop_reason = reason
            break
    if args.steps is not None and step == args.steps and args.steps < cap and stop_reason == "phase_budget_exhausted":
        stop_reason = "explicit_update_limit"
    saved = torch.load(last_checkpoint, map_location="cpu", weights_only=False)
    probe = get_obs(data, data["current"][:8])
    with torch.no_grad():
        before = model.sac_forward(probe, deterministic=True)[0].clone()
        model.load_state_dict(saved["model"], strict=True)
        after = model.sac_forward(probe, deterministic=True)[0]
        if not torch.equal(before, after):
            raise ValueError("Saved checkpoint does not reproduce the final actor exactly")
    result = {"schema": SCHEMA, "phase": args.phase, "identity": identity,
        "completed_step": step, "resumed_from_step": resumed_at, "stop_reason": stop_reason,
        "best_step": monitor["best_step"], "best_checkpoint": str(args.output / "best.pt"),
        "last_checkpoint": str(last_checkpoint), "actor_updates": actor_updates, "critic_updates": critic_updates,
        "inherited_actor_updates": inherited_actor, "inherited_critic_updates": inherited_critic,
        "history": history, "reload_exact": True, "vla_loaded_in_training_process": False,
        "encoder_loaded_in_training_process": False, "deployment_ready": False, "robot_commands": 0,
        "next_phase_started": False, "selection_metric": "validation/expert_episode_macro_mse",
        "validation_role": "development_checkpoint_selection_not_fresh_test",
        "stopping_rule_status": "engineering_budget_and_patience_not_paper_guarantee"}
    write_json(args.output / "result.json", result)
    run.summary.update({key: result[key] for key in ("completed_step", "best_step", "stop_reason", "actor_updates", "critic_updates", "reload_exact", "deployment_ready")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("recipe", "core", "cache", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--phase", required=True, choices=("bc", "q"))
    parser.add_argument("--steps", type=int)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--init-checkpoint", type=Path)
    args = parser.parse_args()
    if args.resume and args.init_checkpoint:
        parser.error("--resume and --init-checkpoint are mutually exclusive")
    if os.environ.get("CUDA_VISIBLE_DEVICES") not in ("4", "5", "6", "7"):
        raise ValueError("Use an explicitly idle authorized training GPU4-7")
    if args.output.exists() and not args.resume:
        raise ValueError("New experiments require a new output directory")
    if args.resume and (not (args.output / "best.pt").is_file()
                        or args.resume.parent.resolve() != args.output.resolve()):
        raise ValueError("Resume must use the existing run directory with its best checkpoint")
    args.output.mkdir(parents=True, exist_ok=bool(args.resume))
    cfg = json.loads(args.recipe.read_text())
    if not cfg.get("freeze_vla") or not cfg.get("freeze_encoder"):
        raise ValueError("This experiment requires frozen B1250 and frozen encoder")
    seed_all(cfg["seed"])
    torch.set_num_threads(4)
    identity = checkpoint_identity(args, cfg)
    run = start_run(args, cfg, identity)
    try:
        train(args, cfg, run, identity)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__ == "__main__":
    main()
