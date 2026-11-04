#!/usr/bin/env python3
"""CPU-only audit/export of an explicitly selected expert-BC or Q checkpoint.

Checks command-space behavior and numerical IK on saved observations. This does
not execute a robot, establish task success, or implement RTC prefix routing.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.eef import decode_prefix, physical_actions, solver
from f1_toys.rlt_replay import digest
from f1_toys.rlt_stage2 import make_policy
from rlt_dagger_train import (SCHEMA, action_metrics, checkpoint_identity, evaluate,
                              normalization, validate_replay)
from rlt_stage2_offline import get_obs, write_json


def parameter_digest(module):
    result = hashlib.sha256()
    for name, value in module.state_dict().items():
        result.update(name.encode())
        result.update(value.detach().cpu().contiguous().numpy().tobytes())
    return result.hexdigest()


def choose_probes(records):
    """Choose first/release-near/last valid chunks without inventing a release.

The release neighborhood is defined by a recorded command crossing below 50
on the physical 0=open, 100=closed scale. It is not a task-success label.
"""
    chosen, segments = {}, []
    for segment in sorted({r["segment_id"] for r in records}):
        indices = sorted((i for i, r in enumerate(records) if r["segment_id"] == segment),
                         key=lambda i: records[i]["decision_ns"])
        first, last = indices[0], indices[-1]
        selections = [(first, "first_valid_transition"), (last, "last_valid_transition")]
        commands = {}
        for i in indices:
            row = records[i]
            if len(row["action_grid_ns"]) != len(row["submitted16"]):
                raise ValueError("Action timestamps and actual commands differ in length")
            for stamp, command in zip(row["action_grid_ns"], row["submitted16"], strict=True):
                item = (float(command[15]), i)
                if stamp in commands and commands[stamp][0] != item[0]:
                    raise ValueError("Conflicting actual gripper targets at one timestamp")
                commands.setdefault(stamp, item)
        ordered = sorted(commands.items())
        release = None
        previous = None
        for stamp, (gripper, index) in ordered:
            if previous is not None and previous[1] >= 50 and gripper < 50:
                release = dict(index=index, first_open_command_ns=stamp,
                               preceding_closed_command_ns=previous[0],
                               command_observation_gap_ns=stamp-previous[0])
                break
            previous = (stamp, gripper)
        if release is None:
            middle = indices[len(indices) // 2]
            selections.append((middle, "middle_no_observed_close_to_open_crossing"))
        else:
            selections.append((release["index"], "recorded_opening_neighborhood"))
        for index, role in selections:
            chosen.setdefault(index, []).append(role)
        segments.append(dict(segment=segment, source_run=records[first]["source_run"],
            split=records[first]["split"], valid_transitions=len(indices),
            recorded_opening=release,
            last_is_true_terminal=bool(records[last]["terminated"]),
            last_is_truncated=bool(records[last]["truncated"])))
    return [{"index": i, "roles": roles} for i, roles in sorted(chosen.items())], segments


def command_differences(candidate, comparator, stats):
    error = np.asarray(candidate) - np.asarray(comparator)
    xyz = error[..., :3] * np.asarray(stats["std"])
    return dict(normalized_mse=float(np.mean(error**2)),
        xyz_axis_mae_mm=float(np.abs(xyz).mean()*1000),
        xyz_l2_mean_mm=float(np.linalg.norm(xyz, axis=-1).mean()*1000),
        rotation6d_mae=float(np.abs(error[..., 3:9]).mean()),
        gripper_normalized_mae=float(np.abs(error[..., 9]).mean()),
        gripper_command_points_mae=float(np.abs(error[..., 9]).mean()*50))


def reference_expert_baseline(reference, data, records, stats, gripper):
    """Score B references against the same real expert actions as the actor."""
    std = torch.as_tensor(stats["std"], dtype=reference.dtype, device=reference.device)
    report = {}
    for split in ("train", "validation"):
        indices = torch.tensor([i for i, row in enumerate(records) if row["split"] == split])
        values = action_metrics(reference[indices], data["actions"][indices], reference[indices],
                                data["intervene_flags"][indices], std, gripper)
        values.pop("reference_mse")
        episodes = []
        for source in sorted({records[i]["source_run"] for i in indices.tolist()}):
            rows = torch.tensor([i for i in indices.tolist() if records[i]["source_run"] == source])
            scored = action_metrics(reference[rows], data["actions"][rows], reference[rows],
                                    data["intervene_flags"][rows], std, gripper)
            scored.pop("reference_mse")
            if scored["expert_steps"]:
                episodes.append({"source_run": source, **scored})
        values["expert_episode_macro_mse"] = float(np.mean([row["expert_mse"] for row in episodes]))
        report[split] = {"metrics": values, "expert_episodes": episodes}
    return report


def verify_cache(args, cfg):
    manifest = json.loads((args.cache / "manifest.json").read_text())
    if manifest["recipe_sha256"] != digest(args.recipe):
        raise ValueError("Cache recipe identity changed")
    for name, expected in manifest["files_sha256"].items():
        if digest(args.cache / name) != expected:
            raise ValueError("Cache file identity changed: " + name)
    records = [json.loads(row) for row in (args.cache / "transitions.jsonl").read_text().splitlines()]
    data = torch.load(args.cache / "replay.pt", map_location="cpu", weights_only=True)
    validate_replay(data, records, cfg)
    count = len(records)
    if len(data["current"]) != count or len(data["next"]) != count:
        raise ValueError("Transition indices and metadata differ in length")
    observations = len(data["z_rl"])
    for name in ("current", "next"):
        value = data[name]
        if value.dtype != torch.long or value.shape != (count,) or (value < 0).any() or (value >= observations).any():
            raise ValueError("Invalid observation lookup: " + name)
    if data["terminated"].dtype != torch.bool or data["truncated"].dtype != torch.bool:
        raise ValueError("Terminal/truncation masks must be bool")
    if data["terminated"].shape != (count,) or data["truncated"].shape != (count,):
        raise ValueError("Terminal/truncation masks must have one value per transition")
    if (data["terminated"] & data["truncated"]).any():
        raise ValueError("A transition cannot both terminate and truncate")
    if not ((data["rewards"] == 0) | (data["rewards"] == 1)).all():
        raise ValueError("Frozen sparse rewards must be binary")
    if not torch.allclose(data["discount"], torch.full_like(data["discount"],
                         cfg["gamma_per_step"]**cfg["model"]["chunk_len"])):
        raise ValueError("Mixed old/new discount contracts")
    for index, row in enumerate(records):
        if row["terminated"] != bool(data["terminated"][index]) or row["truncated"] != bool(data["truncated"][index]):
            raise ValueError("Metadata and tensor boundary flags disagree")
        if not torch.equal(torch.tensor(row["intervene_flags"], dtype=torch.bool), data["intervene_flags"][index]):
            raise ValueError("Metadata and tensor intervention flags disagree")
    return manifest, records, data


def audit(args, cfg, run):
    manifest, records, data = verify_cache(args, cfg)
    stats, gripper = normalization(cfg, manifest)
    checkpoint_sha = digest(args.checkpoint)
    saved = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    identity = checkpoint_identity(args, cfg)
    if saved["schema"] != SCHEMA or saved["identity"] != identity:
        raise ValueError("Checkpoint data/config/code identity changed")
    if saved["phase"] not in ("bc", "q"):
        raise ValueError("Unknown checkpoint training phase")
    if saved.get("deployment_ready") is not False:
        raise ValueError("Expected an undeployed offline checkpoint")
    model = make_policy(args.core, cfg["model"]).eval().requires_grad_(False)
    model.load_state_dict(saved["model"], strict=True)
    target = copy.deepcopy(model.q_head).eval().requires_grad_(False)
    target.load_state_dict(saved["target_critic"], strict=True)
    model_sha, target_sha = parameter_digest(model), parameter_digest(target)
    kinematics = solver()
    parent = Path(cfg["parent_bundle"])
    parent_manifest = json.loads((parent / "bundle.json").read_text())
    if digest(parent / "bundle.json") != manifest["parent_bundle_sha256"]:
        raise ValueError("Frozen B1250 bundle manifest changed")
    for filename in ("contract.json", "norm_stats.json"):
        if digest(parent / filename) != parent_manifest["files_sha256"][filename]:
            raise ValueError("Frozen B1250 metadata changed: " + filename)
    contract = json.loads((parent / "contract.json").read_text())
    if kinematics.model.config_sha256 != contract["kinematics_sha256"]:
        raise ValueError("Frozen parent and IK kinematics differ")
    if json.loads((Path(cfg["encoder"]) / "manifest.json").read_text())["parent_bundle_sha256"] != manifest["parent_bundle_sha256"]:
        raise ValueError("Encoder and cached parent identities differ")
    if digest(Path(cfg["encoder"]) / "manifest.json") != manifest["encoder_manifest_sha256"]:
        raise ValueError("Frozen encoder manifest changed")
    bound_stats = json.loads((parent / "norm_stats.json").read_text())["action"]
    if stats != bound_stats:
        raise ValueError("Cache and B1250 action normalization differ")

    with torch.inference_mode():
        metrics, evaluated = evaluate(model, target, data, records, cfg, stats, gripper, saved["phase"])
        obs = get_obs(data, data["current"])
        chunk, dimension = cfg["model"]["chunk_len"], cfg["model"]["action_dim"]
        actions = {"actual": data["actions"],
                   "reference": model._get_ref_chunk(obs).reshape(-1, chunk, dimension),
                   "candidate": model.sac_forward(obs, deterministic=True)[0].reshape(-1, chunk, dimension)}
        if any(not torch.isfinite(value).all() for value in actions.values()):
            raise ValueError("Nonfinite action predictions")
        reference_baseline = reference_expert_baseline(actions["reference"], data, records, stats, gripper)
        chosen, segment_report = choose_probes(records)
        probes = []
        for probe_number, selection in enumerate(chosen):
            index = selection["index"]
            row = records[index]
            for name, tensor in actions.items():
                normalized = tensor[index].numpy()
                padded = np.zeros((chunk, 32), np.float32)
                padded[:, :dimension] = normalized
                try:
                    decoded = decode_prefix(kinematics, physical_actions(padded, stats),
                        np.asarray(row["state16"]), np.asarray(row["submitted16"][0]), execute_steps=chunk)
                    details = decoded.get("diagnostics", [])
                    result = dict(success=bool(decoded["success"]), reason=decoded.get("reason"),
                        steps_solved=sum(bool(item["success"]) for item in details),
                        gripper_clipped_count=decoded.get("gripper_clipped_count"),
                        maximum_position_error_mm=max((item["position_error_mm"] for item in details), default=None),
                        maximum_rotation_error_deg=max((item["rotation_error_deg"] for item in details), default=None))
                except ValueError as error:
                    result = dict(success=False, reason="invalid_pose_or_command", error=str(error), steps_solved=0)
                probes.append(dict(index=index, roles=selection["roles"], segment=row["segment_id"],
                    source_run=row["source_run"], split=row["split"], action=name, **result,
                    difference_from_actual=command_differences(normalized, actions["actual"][index].numpy(), stats),
                    difference_from_reference=command_differences(normalized, actions["reference"][index].numpy(), stats)))
            if probe_number % 5 == 0 or probe_number+1 == len(chosen):
                print("IK_PROBES", probe_number+1, len(chosen), flush=True)
                run.log({"audit/probe_states_done": probe_number+1}, step=probe_number+1)

        from safetensors.torch import load_file, save_file
        export = args.output / "export"
        export.mkdir(exist_ok=False)
        save_file({name: tensor.detach().contiguous() for name, tensor in model.state_dict().items()}, str(export / "head.safetensors"))
        reloaded = make_policy(args.core, cfg["model"]).eval().requires_grad_(False)
        reloaded.load_state_dict(load_file(str(export / "head.safetensors")), strict=True)
        reloaded_action = reloaded.sac_forward(obs, deterministic=True)[0].reshape_as(actions["candidate"])
        reload_action_exact = torch.equal(reloaded_action, actions["candidate"])
        reload_critic_exact = torch.equal(model.sac_q_forward(obs, data["actions"]),
                                          reloaded.sac_q_forward(obs, data["actions"]))
        if not reload_action_exact or not reload_critic_exact or parameter_digest(model) != parameter_digest(reloaded):
            raise ValueError("Exported head does not reproduce actions/critic/state exactly")
    if parameter_digest(model) != model_sha or parameter_digest(target) != target_sha:
        raise ValueError("Audit modified model parameters")
    if digest(args.checkpoint) != checkpoint_sha:
        raise ValueError("Original checkpoint changed during audit")
    if any(p.grad is not None or p.requires_grad for p in model.parameters()):
        raise ValueError("Audit unexpectedly enabled parameter gradients")
    export_manifest = dict(schema="f1_rlt_stage2_head_v1", model_type="f1_rlt_mlp_eef10_v1",
        phase=saved["phase"], training_phase=saved["phase"],
        q_guidance_updates=saved["actor_updates"] if saved["phase"] == "q" else 0,
        critic_not_validated_for_policy_optimization=True,
        checkpoint_step=saved["step"], checkpoint_sha256=checkpoint_sha,
        checkpoint_run_id=saved["run_id"], source_checkpoint=str(args.checkpoint),
        head_sha256=digest(export / "head.safetensors"), model_config=cfg["model"],
        parent_bundle_sha256=manifest["parent_bundle_sha256"],
        encoder_manifest_sha256=manifest["encoder_manifest_sha256"],
        recipe_sha256=identity["recipe_sha256"], replay_manifest_sha256=identity["replay_manifest_sha256"],
        output_parameterization=cfg["output_parameterization"], reference_mode=cfg["reference_mode"],
        adapter_sha256=identity["adapter_sha256"], core_rlt_policy_sha256=identity["core_rlt_policy_sha256"],
        action_norm_stats=stats, gripper_encoding=gripper,
        q_branch_has_new_updates=saved["phase"] == "q" and saved["step"] > 0,
        actor_updates=saved["actor_updates"], critic_updates=saved["critic_updates"],
        inherited_actor_updates=saved["inherited_actor_updates"], inherited_critic_updates=saved["inherited_critic_updates"],
        deployment_ready=False, rtc_prefix_routing_implemented=False, asynchronous_runner=False,
        export_reload_action_exact=True, export_reload_critic_exact=True)
    write_json(export / "manifest.json", export_manifest)
    counts = {name: dict(success=sum(row["success"] for row in probes if row["action"] == name),
                         total=sum(row["action"] == name for row in probes)) for name in actions}
    report = dict(schema="f1_rlt_dagger_candidate_audit_v1", checkpoint=export_manifest,
        audit_source_sha256=digest(__file__), evaluation=evaluated, probe_selection=segment_report,
        reference_expert_baseline=reference_baseline,
        ik_probes=probes, ik_counts=counts, probe_states=len(chosen),
        checks=dict(cache_hashes_verified=True, checkpoint_identity_verified=True,
                    parameters_unchanged=True, source_checkpoint_unchanged=True,
                    export_reload_action_exact=reload_action_exact, export_reload_critic_exact=reload_critic_exact),
        parameter_updates=0, robot_commands=0, device="cpu", wandb_url=run.url,
        deployment_ready=False, validation_scope=cfg.get("validation_scope"),
        limitations=[
            "Numerical IK does not test contact, collision safety, RTC, tracking, task success, or recovery.",
            "Recorded command opening is a probe-selection convention, not confirmed placement success.",
            "Last valid transitions need not be true task terminals; flags are reported per segment.",
            "Candidate/reference Q at logged terminals is counterfactual; only actual behavior has a fixed target.",
            "The supplied checkpoint was audited, not automatically selected or promoted.",
            "A Q checkpoint at step zero is inherited BC, not a learned Q improvement.",
            "XYZ errors describe command targets in the shared observation anchor, not measured robot tracking.",
            "Reference was recomputed without RTC; committed-prefix routing remains unimplemented."])
    write_json(args.output / "report.json", report)
    table = __import__("wandb").Table(columns=["segment", "roles", "action", "success", "steps_solved", "reason"])
    for row in probes:
        table.add_data(row["segment"], ",".join(row["roles"]), row["action"], row["success"], row["steps_solved"], row.get("reason"))
    run.log({**metrics, "audit/ik_probes": table}, step=len(chosen)+1)
    run.summary.update({f"reference_baseline/{split}/{key}": value
        for split, item in reference_baseline.items() for key, value in item["metrics"].items()
        if value is not None})
    run.summary.update(dict(parameter_updates=0, robot_commands=0, deployment_ready=False,
        checkpoint_step=saved["step"], phase=saved["phase"],
        export_reload_action_exact=True, export_reload_critic_exact=True,
        **{f"ik_{name}_success": value["success"] for name, value in counts.items()},
        ik_probes_per_action=len(chosen)))
    print(json.dumps(dict(output=str(args.output), checkpoint_step=saved["step"], phase=saved["phase"],
                          ik_counts=counts, wandb_url=run.url), indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("recipe", "core", "cache", "checkpoint", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cfg = json.loads(args.recipe.read_text())
    if not cfg.get("freeze_vla") or not cfg.get("freeze_encoder"):
        raise ValueError("Audit requires the frozen-parent recipe")
    if cfg["model"]["action_dim"] != 10:
        raise ValueError("This auditor requires F1 EEF10")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    import wandb
    spec = cfg["wandb"]
    run = wandb.init(project=spec["project"], entity=spec["entity"], group=spec["group"],
        id=args.run_id, resume="never", name=cfg["experiment"]+"_candidate_audit",
        job_type="offline-candidate-audit", tags=["F1", "RLT_Stage2", "expert_DAgger", "zero_update", "cpu"],
        dir=str(args.output), mode=os.environ.get("WANDB_MODE", "online"),
        config=dict(recipe_sha256=digest(args.recipe), checkpoint_sha256=digest(args.checkpoint),
                    audit_source_sha256=digest(__file__), parameter_updates=0, device="cpu"))
    write_json(args.output / "wandb.json", dict(id=run.id, url=run.url, mode=run.settings.mode))
    try:
        audit(args, cfg, run)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__ == "__main__":
    main()
