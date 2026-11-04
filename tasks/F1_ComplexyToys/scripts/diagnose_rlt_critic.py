#!/usr/bin/env python3
"""Inspect frozen BC critic inputs, counterfactual sensitivity and gradients.

Autograd is used only to measure local loss gradients. No optimizer is created,
no parameter update is made, and no robot or inference service is contacted.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import torch
import torch.nn.functional as F


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            value.update(block)
    return value.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n")


def state_sha(model):
    value = hashlib.sha256()
    for name, tensor in model.state_dict().items():
        value.update(name.encode())
        value.update(tensor.detach().cpu().contiguous().numpy().tobytes())
    return value.hexdigest()


def sampling_probabilities(records, terminal_probability):
    """Exact marginal probabilities of the frozen segment/terminal sampler."""
    result = torch.zeros(len(records), dtype=torch.float64)
    segments = sorted({r["segment_id"] for r in records if r["split"] == "train"})
    terminals = [i for i, r in enumerate(records) if r["split"] == "train" and r["terminated"]]
    terminal_probability = terminal_probability if terminals else 0.0
    for segment in segments:
        indices = [i for i, r in enumerate(records) if r["split"] == "train" and r["segment_id"] == segment]
        result[indices] += (1-terminal_probability)/(len(segments)*len(indices))
    if terminals:
        result[terminals] += terminal_probability/len(terminals)
    if not torch.isclose(result.sum(), torch.tensor(1., dtype=result.dtype)):
        raise ValueError("Expected training sampling probabilities to sum to one")
    return result


def cosine(first, second):
    # Million-element float32 reductions can even report self-cosine > 1.
    first, second = first.double(), second.double()
    denominator = first.norm()*second.norm()
    return float((torch.dot(first, second)/denominator).clamp(-1., 1.)) if denominator > 0 else None


def vector_stats(value):
    value = value.float()
    return dict(mean=float(value.mean()), rms=float(value.square().mean().sqrt()),
                min=float(value.min()), max=float(value.max()), abs_max=float(value.abs().max()))


def feature_stats(value):
    value = value.flatten(1).float()
    return dict(dimension=value.shape[1], rows=len(value), **vector_stats(value),
        mean_row_l2=float(value.norm(dim=-1).mean()),
        centered_across_rows_rms=float((value-value.mean(0)).square().mean().sqrt()),
        constant_dimensions=int((value.max(0).values == value.min(0).values).sum()))


def distances(features):
    result = {}
    for name, value in features.items():
        value = value.flatten(1).float()
        difference = value[:, None]-value[None, :]
        result[name] = dict(rmse=difference.square().mean(-1).sqrt().tolist(),
                            cosine=(F.normalize(value, dim=-1)@F.normalize(value, dim=-1).T).tolist())
    return result


def swap_inputs(features, group):
    """Rows are recipients, columns donors; other input groups stay fixed."""
    count = len(next(iter(features.values())))
    result = {}
    for name, value in features.items():
        owner = value[:, None].expand(count, count, -1)
        donor = value[None, :].expand(count, count, -1)
        result[name] = (donor if name in group else owner).reshape(count*count, -1)
    return result


def run_diagnosis(args, cfg, run):
    sys.path[:0] = [str(args.task_root/"src"), str(args.task_root/"scripts")]
    from f1_toys.rlt_stage2 import make_policy, td_target
    from f1_toys.eef import encode_actions, normalize_actions, solver
    from f1_toys.processing import Processor
    from rlt_dagger_train import SCHEMA, checkpoint_identity, discounted_rewards, validate_replay
    from rlt_stage2_offline import get_obs

    manifest_path = args.cache/"manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest["recipe_sha256"] != sha(args.recipe):
        raise ValueError("Cache/recipe identity mismatch")
    for name, expected in manifest["files_sha256"].items():
        if sha(args.cache/name) != expected:
            raise ValueError("Cached input changed: "+name)
    records = [json.loads(row) for row in (args.cache/"transitions.jsonl").read_text().splitlines()]
    data = torch.load(args.cache/"replay.pt", map_location="cpu", weights_only=True)
    validate_replay(data, records, cfg)
    source_hash = sha(args.checkpoint)
    saved = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    if saved["schema"] != SCHEMA or saved["identity"] != checkpoint_identity(args, cfg) or saved["phase"] != "bc":
        raise ValueError("Expected the exact frozen BC checkpoint/data/code")
    model = make_policy(args.core, cfg["model"]).eval().requires_grad_(False)
    model.load_state_dict(saved["model"], strict=True)
    target = copy.deepcopy(model.q_head).eval().requires_grad_(False)
    target.load_state_dict(saved["target_critic"], strict=True)
    model_hash, target_hash = state_sha(model), state_sha(target)
    obs, nxt = get_obs(data, data["current"]), get_obs(data, data["next"])
    terms = [i for i, r in enumerate(records) if r["terminated"]]
    zdim, pdim = cfg["model"]["z_dim"], cfg["model"]["proprio_dim"]
    features = {"token": obs["z_rl"], "proprio": obs["proprio"], "action": data["actions"].flatten(1)}
    terminal_features = {name: value[terms] for name, value in features.items()}
    terminal_features["full_input"] = torch.cat(list(terminal_features.values()), dim=-1)
    masks = data["intervene_flags"]
    kind = ["human" if bool(flags.all()) else "autonomous" if not bool(flags.any()) else "mixed" for flags in masks]
    probabilities = sampling_probabilities(records, cfg.get("terminal_sample_probability", .1))
    with torch.no_grad():
        reward = discounted_rewards(data, cfg)
        next_q = target(model._critic_state(nxt), model._get_ref_chunk(nxt)).min(-1, keepdim=True).values
        targets = td_target(reward, data["terminated"][:, None], data["discount"][:, None], next_q)
        q = model.sac_q_forward(obs, data["actions"])
    assert torch.equal(targets[data["terminated"]], reward[data["terminated"]])
    assert not (data["terminated"] & data["truncated"]).any()
    assert torch.equal(model._critic_state(obs), torch.cat((features["token"], features["proprio"]), -1))

    # Reconstruct stored right-only proprio and EEF10 from original metadata.
    parent = Path(cfg["parent_bundle"])
    assert sha(parent/"bundle.json") == manifest["parent_bundle_sha256"]
    stats = json.loads((parent/"norm_stats.json").read_text())
    contract = json.loads((parent/"contract.json").read_text())
    assert contract["active_state_indices"] == [7, 8, 9, 10, 11, 12, 13, 15]
    processor = Processor(contract, stats, None)
    proprio_error = 0.0
    inactive_nonzero = 0
    for i, row in enumerate(records):
        for prefix, index_name in (("", "current"), ("next_", "next")):
            state = processor.model_state(np.asarray(row[prefix+"state16"]))
            inactive_nonzero += np.count_nonzero(state[[0,1,2,3,4,5,6,14]+list(range(16,32))])
            expected = torch.from_numpy(state[[7,8,9,10,11,12,13,15]])
            proprio_error = max(proprio_error, float((expected-data["proprio"][data[index_name][i]]).abs().max()))
    kinematics = solver().model
    assert kinematics.config_sha256 == contract["kinematics_sha256"]
    action_error = 0.0
    for i in terms:
        encoded = encode_actions(kinematics, records[i]["state16"], np.asarray(records[i]["submitted16"]))
        expected = torch.from_numpy(normalize_actions(encoded, stats["action"])[..., :10])
        action_error = max(action_error, float((expected-data["actions"][i]).abs().max()))
    assert proprio_error < 1e-6 and action_error < 1e-6 and inactive_nonzero == 0
    labels = []
    for i in terms:
        labels.append(dict(index=i, source_run=records[i]["source_run"], segment=records[i]["segment_id"],
            split=records[i]["split"], source=kind[i], outcome=records[i]["segment_outcome"],
            observation_id=records[i]["observation_id"], next_observation_id=records[i]["next_observation_id"],
            target=float(targets[i, 0]), q=q[i].tolist(), training_sample_probability=float(probabilities[i]),
            input_scales={name: vector_stats(value[i]) for name, value in features.items()}))
    write(args.output/"terminal-inputs.json", dict(labels=labels, values={k:v.tolist() for k,v in terminal_features.items()}))
    pairwise = distances(terminal_features)
    nearest = []
    for local, row in enumerate(labels):
        if row["split"] != "validation":
            continue
        entry = dict(index=row["index"], outcome=row["outcome"], by_input={})
        for name, value in pairwise.items():
            ranked = sorted((value["rmse"][local][j], j) for j,t in enumerate(labels) if t["split"] == "train")[:3]
            entry["by_input"][name] = [dict(index=labels[j]["index"], source=labels[j]["source"],
                outcome=labels[j]["outcome"], rmse=distance, cosine=value["cosine"][local][j]) for distance,j in ranked]
        nearest.append(entry)
    write(args.output/"terminal-pairwise.json", dict(labels=labels, metrics=pairwise, nearest_train_terminals=nearest))

    first_layer = {}
    ranges = {"token": (0,zdim), "proprio": (zdim,zdim+pdim), "action": (zdim+pdim,zdim+pdim+features["action"].shape[1])}
    with torch.no_grad():
        for head_number, head in enumerate(model.q_head.qs):
            assert not head.train_action_encoder
            linear = head.net[0]
            assert isinstance(linear, torch.nn.Linear) and linear.in_features == zdim+pdim+features["action"].shape[1]
            contributions = {name: features[name][terms]@linear.weight[:,lo:hi].T for name,(lo,hi) in ranges.items()}
            combined = sum(contributions.values())+linear.bias
            expected = linear(terminal_features["full_input"])
            assert torch.allclose(combined, expected, atol=2e-5, rtol=1e-5)
            first_layer[str(head_number)] = dict(
                modules=[type(layer).__name__ for layer in head.net], input_layout=ranges,
                contributions={name: feature_stats(value) for name,value in contributions.items()},
                bias=vector_stats(linear.bias), combined=feature_stats(combined),
                weight_rms={name: float(linear.weight[:,lo:hi].square().mean().sqrt()) for name,(lo,hi) in ranges.items()},
                maximum_decomposition_error=float((combined-expected).abs().max()))
        swaps = {}
        terminal_only = {name: value[terms] for name,value in features.items()}
        for names in (("token",), ("proprio",), ("action",), ("token","proprio"), ("token","proprio","action")):
            altered = swap_inputs(terminal_only, names)
            predicted = model.q_head(torch.cat((altered["token"],altered["proprio"]),-1), altered["action"])
            predicted = predicted.reshape(len(terms),len(terms),2)
            assert torch.allclose(predicted[torch.arange(len(terms)),torch.arange(len(terms))],q[terms],atol=1e-6)
            if len(names)==3:
                assert torch.allclose(predicted,q[terms][None].expand_as(predicted),atol=1e-6)
            delta = predicted-q[terms][:,None]
            opposite = torch.tensor([[a["outcome"]!=b["outcome"] for b in labels] for a in labels])
            swaps["+".join(names)] = dict(q=predicted.tolist(), delta_from_recipient=delta.tolist(),
                absolute_delta=vector_stats(delta.abs()), cross_outcome_absolute_delta=vector_stats(delta[opposite].abs()),
                per_recipient_max_abs_delta=delta.abs().amax((1,2)).tolist())
    write(args.output/"terminal-swaps.json", dict(labels=labels, interventions=swaps,
        interpretation="Sensitivity only: fixed donor-group substitution may create physically inconsistent/off-distribution inputs; no counterfactual outcome is known."))
    print("INPUTS_AND_SWAPS_DONE",len(terms),flush=True)

    # Compute gradients of conditional cohort losses. Only training cohorts have
    # a sampler mass; validation gradients are diagnostics, never training data.
    cohorts = {}
    for split in ("train","validation"):
        for boundary in ("terminal","nonterminal"):
            indices = [i for i,r in enumerate(records) if r["split"]==split and r["terminated"]==(boundary=="terminal")]
            if indices:
                cohorts[f"{split}/{boundary}/all"] = indices
            for source,outcome in itertools.product(("human","autonomous"),("success","failure","truncated")):
                selected = [i for i in indices if kind[i]==source and records[i]["segment_outcome"]==outcome]
                if selected:
                    cohorts[f"{split}/{boundary}/{source}/{outcome}"] = selected
    model.q_head.requires_grad_(True)
    parameters = list(model.q_head.parameters())
    parameter_names = [name for name,_ in model.q_head.named_parameters()]
    gradients, gradient_report = {}, {}
    for number,(name,indices) in enumerate(cohorts.items()):
        ix = torch.tensor(indices)
        weight = probabilities[ix].float() if name.startswith("train/") else torch.ones(len(ix))
        mass = float(weight.sum()) if name.startswith("train/") else None
        weight = weight/weight.sum()
        pred = model.sac_q_forward({key:value[ix] for key,value in obs.items()},data["actions"][ix])
        error = (pred-targets[ix]).square().mean(-1)
        loss = (error*weight).sum()
        raw = torch.autograd.grad(loss,parameters)
        vector = torch.cat([value.detach().flatten() for value in raw])
        gradients[name] = vector
        layer_grad = {}
        for pname,value in zip(parameter_names,raw,strict=True):
            if pname.endswith("net.0.weight"):
                layer_grad[pname] = {part:float(value[:,lo:hi].norm()) for part,(lo,hi) in ranges.items()}
        gradient_report[name] = dict(count=len(ix), training_sampling_mass=mass, conditional_loss=float(loss.detach()),
            conditional_gradient_l2=float(vector.norm()),
            contribution_gradient_l2=float(vector.norm())*mass if mass is not None else None,
            mean_target=float((targets[ix,0]*weight).sum()), mean_actual_q=float((pred.detach().mean(-1)*weight).sum()),
            first_layer_gradient_l2_by_input=layer_grad)
        run.log({"diagnosis/gradient_cohorts_done":number+1},step=number+1)
    terminal_mass = float(probabilities[data["terminated"]].sum())
    nonterminal_mass = 1-terminal_mass
    expected_gradient = terminal_mass*gradients["train/terminal/all"]+nonterminal_mass*gradients["train/nonterminal/all"]
    # Independent full loss derivative verifies the conditional weighting math.
    all_train = torch.tensor([i for i,r in enumerate(records) if r["split"]=="train"])
    pred = model.sac_q_forward({key:value[all_train] for key,value in obs.items()},data["actions"][all_train])
    full_loss = ((pred-targets[all_train]).square().mean(-1)*probabilities[all_train].float()).sum()
    full_gradient = torch.cat([g.detach().flatten() for g in torch.autograd.grad(full_loss,parameters)])
    relative_error = float((full_gradient-expected_gradient).norm()/full_gradient.norm().clamp(min=1e-12))
    assert relative_error < 1e-4
    gradients["train/full_expected_objective"] = full_gradient
    gradient_report["train/full_expected_objective"] = dict(count=len(all_train),training_sampling_mass=1.,
        conditional_loss=float(full_loss.detach()), conditional_gradient_l2=float(full_gradient.norm()),
        contribution_gradient_l2=float(full_gradient.norm()))
    names = list(gradients)
    gradient_cosines = {first:{second:cosine(gradients[first],gradients[second]) for second in names} for first in names}
    model.q_head.requires_grad_(False)
    assert all(parameter.grad is None for parameter in model.parameters())
    write(args.output/"gradient-cohorts.json",dict(cohorts=gradient_report,cosines=gradient_cosines,
        terminal_sampling_mass=terminal_mass, nonterminal_sampling_mass=nonterminal_mass,
        full_gradient_reconstruction_relative_error=relative_error,
        interpretation="Instantaneous raw loss-gradient geometry, not optimizer steps; Adam momentum/preconditioning and future training may change actual update effects."))

    assert state_sha(model)==model_hash and state_sha(target)==target_hash
    assert sha(args.checkpoint)==source_hash
    assert all(parameter.grad is None and not parameter.requires_grad for parameter in model.parameters())
    summary = dict(schema="f1_rlt_critic_zero_update_diagnosis_v1",checkpoint_sha256=source_hash,
        checkpoint_phase=saved["phase"], checkpoint_step=saved["step"],
        cache_manifest_sha256=sha(manifest_path),source_sha256=sha(__file__),records=len(records),
        terminal_counts={split:sum(row["split"]==split for row in labels) for split in ("train","validation")},
        labels=labels, feature_scales={name:feature_stats(value) for name,value in features.items()},
        terminal_feature_scales={name:feature_stats(value) for name,value in terminal_features.items()},
        terminal_next_token_scales=feature_stats(nxt["z_rl"][terms]),
        first_layer=first_layer, nearest_train_terminals=nearest,
        swap_absolute_delta={name:value["absolute_delta"] for name,value in swaps.items()},
        gradient_cohorts=gradient_report,
        terminal_vs_nonterminal_gradient_cosine=gradient_cosines["train/terminal/all"]["train/nonterminal/all"],
        full_gradient_alignment={name:gradient_cosines["train/full_expected_objective"][name] for name in names},
        terminal_sampling_mass=terminal_mass,
        checks=dict(cache_hashes=True,checkpoint_identity=True,terminal_bootstrap_zero=True,
            right_proprio_reconstruction_max_abs_error=proprio_error,
            terminal_eef_action_reconstruction_max_abs_error=action_error,
            inactive_model_state_nonzero_count=int(inactive_nonzero),
            critic_concat_layout_verified=True,swap_identity_verified=True,
            gradient_probability_reconstruction_relative_error=relative_error,
            parameters_unchanged=True,checkpoint_file_unchanged=True,no_parameter_grad_accumulation=True),
        parameter_updates=0,optimizer_constructed=False,robot_commands=0,device="cpu",wandb_url=run.url,
        limitations=["Token/proprio are the observations before the final chunk, not the post-outcome terminal images.",
            "Feature proximity and counterfactual sensitivity do not identify a causal training failure.",
            "Input swaps may be off-distribution and have no observed counterfactual outcomes.",
            "Gradient conflicts are local to BC400 and the saved target network; no optimizer step was performed.",
            "Validation gradients are diagnostic only and were not used to train or select new parameters.",
            "Existing development validation is tiny and has prior model-selection exposure.",
            "Right proprio/action reconstruction checks do not prove visual token task sufficiency or image semantics.",
            "No online actor, RTC integration, deployment, or robot action is tested."])
    write(args.output/"report.json",summary)
    table=__import__("wandb").Table(columns=["index","split","source","outcome","target","q1","q2"])
    for row in labels:
        table.add_data(row["index"],row["split"],row["source"],row["outcome"],row["target"],*row["q"])
    run.log({"diagnosis/terminals":table,"diagnosis/terminal_vs_nonterminal_gradient_cosine":summary["terminal_vs_nonterminal_gradient_cosine"]},step=len(cohorts)+1)
    run.summary.update(dict(parameter_updates=0,optimizer_constructed=False,robot_commands=0,
        parameters_unchanged=True,checkpoint_step=saved["step"],terminal_count=len(terms),
        terminal_vs_nonterminal_gradient_cosine=summary["terminal_vs_nonterminal_gradient_cosine"]))
    print(json.dumps(dict(output=str(args.output),terminal_counts=summary["terminal_counts"],
        terminal_vs_nonterminal_gradient_cosine=summary["terminal_vs_nonterminal_gradient_cosine"],
        wandb_url=run.url),indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("task-root","recipe","core","cache","checkpoint","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--run-id",required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    os.environ["CUDA_VISIBLE_DEVICES"]=""
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    cfg=json.loads(args.recipe.read_text())
    import wandb
    run=wandb.init(project=cfg["wandb"]["project"],entity=cfg["wandb"]["entity"],
        group="10_RLT_Training_Diagnostics",job_type="critic-zero-update-diagnosis",id=args.run_id,
        name="B1250_RLT_BC400_Critic_Inputs_Gradients",resume="never",dir=str(args.output),
        mode=os.environ.get("WANDB_MODE","online"),tags=["F1","RLT_Stage2","BC400","critic","zero_update","cpu"],
        config=dict(checkpoint_sha256=sha(args.checkpoint),recipe_sha256=sha(args.recipe),
                    script_sha256=sha(__file__),parameter_updates=0,optimizer_constructed=False))
    write(args.output/"wandb.json",dict(id=run.id,url=run.url,mode=run.settings.mode))
    try:
        run_diagnosis(args,cfg,run)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__=="__main__":
    main()
