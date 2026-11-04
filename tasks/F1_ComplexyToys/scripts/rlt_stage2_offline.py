#!/usr/bin/env python3
"""Audit five saved F1 recordings, cache frozen features, train offline heads.

This entrypoint has no robot transport or async runner. A produced checkpoint
is explicitly not deployable until RTC action routing and live validation exist.
"""
from __future__ import annotations

import argparse
import copy
import io
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
from f1_toys.rlt_replay import audit_raw, contained, digest, select_segment
from f1_toys.rlt_stage2 import (actor_loss, actor_parameters, check_source_split,
    make_policy, parameter_counts, td_target, update_target)


def write_json(path, value):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def seed_all(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def start_run(args, cfg):
    import wandb
    spec = cfg["wandb"]
    run = wandb.init(project=spec["project"], entity=spec["entity"], id=args.run_id,
        name=cfg["experiment"] + "_" + args.job, job_type=args.job,
        group="00_Engineering_Checks" if args.job == "preflight" else spec["group"],
        tags=["F1", "RLT_Stage2", "frozen_B1250", "five_raw_recordings", "offline", args.job],
        resume="must" if args.resume else "never", dir=str(args.output),
        mode=os.environ.get("WANDB_MODE", "online"), config={"recipe": cfg,
        "recipe_sha256": digest(args.recipe), "runner_sha256": digest(__file__),
        "code_manifest_sha256": digest(TASK.parents[1] / "SOURCE_MANIFEST.json")})
    write_json(args.output / "wandb.json", {"id": run.id, "url": run.url, "mode": run.settings.mode})
    return run


def read_images(root, frame_row):
    images = {}
    for frame in frame_row["frames"]:
        if frame["camera"] not in ("head", "right_wrist"):
            continue
        with contained(root / "payload", frame["path"]).open("rb") as stream:
            stream.seek(frame["payload_offset"])
            blob = stream.read(frame["payload_bytes"])
        import hashlib
        if hashlib.sha256(blob).hexdigest() != frame["payload_sha256"]:
            raise ValueError("Selected image hash mismatch")
        image = Image.open(io.BytesIO(blob))
        image.load()
        if image.mode != "RGB" or image.size != (frame["width"], frame["height"]):
            raise ValueError("Original image geometry mismatch")
        images[frame["camera"]] = image
    return images


@torch.no_grad()
def extract_feature(policy, reader, frame, root, seed):
    from f1_toys.inference import batched_observation
    images = read_images(root, frame)
    data = policy.processor.observation(images, np.asarray(frame["state16"], float))
    observation = batched_observation(data, policy.observation_type, policy.device)
    original = policy.model.build_prefix_cache
    captured = []
    def capture(obs):
        prefix, mask, kv = original(obs)
        captured.append((prefix, mask))
        return prefix, mask, kv
    policy.model.build_prefix_cache = capture
    try:
        with torch.autocast("cuda", dtype=torch.bfloat16):
            reference = policy.model.sample_actions(observation, num_steps=5,
                rng=torch.Generator(device=policy.device).manual_seed(seed))
            if len(captured) != 1:
                raise ValueError("Expected one shared VLA prefix for reference and token")
            z = reader.encoder(*captured[0]).flatten(1).float()
    finally:
        del policy.model.build_prefix_cache
    if reference.shape != (1, 50, 32) or torch.count_nonzero(reference[..., 10:]):
        raise ValueError("F1 reference contract mismatch")
    return {"z_rl": z[0].cpu(), "proprio": observation.state[0, list(range(7,14)) + [15]].float().cpu(),
            "ref_chunk": reference[0, :, :10].float().cpu()}


def prepare(args, cfg, run):
    from f1_toys.eef import encode_actions, normalize_actions, solver
    from f1_toys.inference import Policy
    from f1_toys.rlt import FrozenTokenReader
    records, reports, receipts, raw = [], [], {}, {}
    check_source_split(cfg["segments"])
    for name in sorted({s["source_run"] for s in cfg["segments"]}):
        root = Path(cfg["raw_root"]) / name
        streams, payload, receipt = audit_raw(root)
        raw[name] = (root, {r["observation_id"]: r for r in payload})
        receipts[name] = receipt
        for segment in (s for s in cfg["segments"] if s["source_run"] == name):
            selected, report = select_segment(streams, payload, segment, cfg)
            records.extend(selected)
            reports.append(report)
        print("RAW_AUDITED", name, flush=True)
    if not records or sum(sum(r["rewards"]) for r in records) < 1:
        raise ValueError("A verified positive transition is required for this pilot")
    if {r["source_run"] for r in records} != {s["source_run"] for s in cfg["segments"]}:
        raise ValueError("Some requested raw recordings have no valid transitions")
    write_json(args.output / "audit.json", {"segments": reports, "sources": receipts,
        "records": len(records), "split_groups": check_source_split(records),
        "raw_modified": False, "robot_commands": 0})
    (args.output / "transitions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    policy = Policy(cfg["parent_bundle"], args.core)
    policy.model.requires_grad_(False)
    reader = FrozenTokenReader(cfg["encoder"], policy, args.core)
    model = solver().model
    if model.config_sha256 != policy.contract["kinematics_sha256"]:
        raise ValueError("Kinematics identity mismatch")
    keys = sorted({(r["source_run"], r[k]) for r in records for k in ("observation_id", "next_observation_id")})
    lookup = {key: i for i, key in enumerate(keys)}
    features = []
    start = time.monotonic()
    for i, (name, oid) in enumerate(keys):
        root, frames = raw[name]
        feature = extract_feature(policy, reader, frames[oid], root, cfg["seed"] + i)
        if not all(torch.isfinite(v).all() for v in feature.values()):
            raise ValueError("Nonfinite frozen features")
        features.append(feature)
        if i % 20 == 0 or i + 1 == len(keys):
            run.log({"cache/observations": i+1, "cache/total": len(keys),
                     "cache/observations_per_second": (i+1)/(time.monotonic()-start)}, step=i+1)
            print("FEATURES", i+1, len(keys), flush=True)
    actions = []
    for record in records:
        action = encode_actions(model, record["state16"], np.asarray(record["submitted16"]))
        actions.append(normalize_actions(action, policy.stats["action"])[..., :10])
    arrays = {k: torch.stack([f[k] for f in features]) for k in features[0]}
    arrays.update(actions=torch.as_tensor(np.stack(actions)),
        current=torch.tensor([lookup[(r["source_run"], r["observation_id"])] for r in records]),
        next=torch.tensor([lookup[(r["source_run"], r["next_observation_id"])] for r in records]),
        rewards=torch.tensor([r["rewards"] for r in records]),
        terminated=torch.tensor([r["terminated"] for r in records]),
        truncated=torch.tensor([r["truncated"] for r in records]),
        discount=torch.tensor([r["discount"] for r in records]))
    torch.save(arrays, args.output / "replay.pt")
    assert all(p.grad is None and not p.requires_grad for p in policy.model.parameters())
    assert all(p.grad is None and not p.requires_grad for p in reader.encoder.parameters())
    manifest = {"schema": "f1_rlt_offline_replay_v1", "recipe_sha256": digest(args.recipe),
        "parent_bundle_sha256": policy.bundle_id, "encoder_manifest_sha256": digest(Path(cfg["encoder"])/"manifest.json"),
        "records": len(records), "observations": len(features), "source_runs": len(raw),
        "reference_mode": cfg["reference_mode"], "reference_is_original_executed_proposal": False,
        "files_sha256": {n: digest(args.output/n) for n in ("audit.json", "transitions.jsonl", "replay.pt")},
        "sources": receipts, "vla_frozen": True, "encoder_frozen": True,
        "deployment_ready": False, "robot_commands": 0}
    write_json(args.output / "manifest.json", manifest)
    run.summary.update({"records": len(records), "observations": len(features), "vla_frozen": True})


def get_obs(data, indices):
    return {k: data[k][indices] for k in ("z_rl", "proprio", "ref_chunk")}


@torch.no_grad()
def evaluate(model, target, data, records, cfg):
    result = {}
    rewards = (data["rewards"] * cfg["gamma_per_step"] ** torch.arange(cfg["model"]["chunk_len"], device="cuda")).sum(-1, keepdim=True)
    for split in ("train", "validation"):
        ix = torch.tensor([i for i,r in enumerate(records) if r["split"] == split], device="cuda")
        obs, nxt = get_obs(data, data["current"][ix]), get_obs(data, data["next"][ix])
        pi = model.sac_forward(obs, deterministic=True)[0]
        ref = model._get_ref_chunk(obs)
        next_pi = model.sac_forward(nxt, deterministic=True)[0]
        next_q = target(model._critic_state(nxt), next_pi).min(-1, keepdim=True).values
        y = td_target(rewards[ix], data["terminated"][ix,None], data["discount"][ix,None], next_q)
        q = model.sac_q_forward(obs, data["actions"][ix])
        error = (pi-ref).reshape(-1,cfg["model"]["chunk_len"],10)
        result.update({split+"/reference_mse": error.square().mean().item(),
            split+"/xyz_reference_mae": error[...,:3].abs().mean().item(),
            split+"/gripper_reference_mae": error[...,9].abs().mean().item(),
            split+"/critic_td_mse": (q-y).square().mean().item(),
            split+"/q_mean": q.mean().item(), split+"/q_abs_max": q.abs().max().item()})
    return result


def train(args, cfg, run):
    manifest = json.loads((args.cache/"manifest.json").read_text())
    if manifest["recipe_sha256"] != digest(args.recipe):
        raise ValueError("Replay/config identity mismatch")
    for name, expected in manifest["files_sha256"].items():
        if digest(args.cache/name) != expected:
            raise ValueError("Replay data changed")
    records = [json.loads(s) for s in (args.cache/"transitions.jsonl").read_text().splitlines()]
    check_source_split(records)
    data = {k:v.cuda() for k,v in torch.load(args.cache/"replay.pt", weights_only=True).items()}
    model = make_policy(args.core, cfg["model"]).cuda()
    target = copy.deepcopy(model.q_head).eval().requires_grad_(False)
    actor_optim = torch.optim.Adam(actor_parameters(model), lr=cfg["learning_rate"])
    critic_optim = torch.optim.Adam(model.q_head.parameters(), lr=cfg["learning_rate"])
    rng = np.random.default_rng(cfg["seed"])
    step, history = 0, []
    if args.resume:
        state = torch.load(args.resume, map_location="cpu", weights_only=False)
        if state["recipe_sha256"] != digest(args.recipe) or state["replay_manifest_sha256"] != digest(args.cache/"manifest.json"):
            raise ValueError("Resume identities changed")
        model.load_state_dict(state["model"], strict=True)
        target.load_state_dict(state["target_critic"], strict=True)
        actor_optim.load_state_dict(state["actor_optimizer"])
        critic_optim.load_state_dict(state["critic_optimizer"])
        rng.bit_generator.state = state["numpy_rng"]
        torch.set_rng_state(state["torch_rng"])
        torch.cuda.set_rng_state(state["cuda_rng"])
        step, history = state["step"], state["history"]
    counts = parameter_counts(model)
    write_json(args.output/"architecture.json", counts)
    run.summary.update(counts)
    groups = sorted({r["segment_id"] for r in records if r["split"] == "train"})
    grouped = {g: np.array([i for i,r in enumerate(records) if r["split"]=="train" and r["segment_id"]==g]) for g in groups}
    terminals = np.array([i for i,r in enumerate(records) if r["split"]=="train" and r["terminated"]])
    rewards = (data["rewards"] * cfg["gamma_per_step"] ** torch.arange(cfg["model"]["chunk_len"], device="cuda")).sum(-1,keepdim=True)
    started = time.monotonic()
    max_steps = args.steps if args.steps is not None else cfg["max_steps"]

    def save(at):
        path = args.output/f"step-{at:06d}.pt"
        state = {"model": model.state_dict(), "target_critic": target.state_dict(),
            "actor_optimizer": actor_optim.state_dict(), "critic_optimizer": critic_optim.state_dict(),
            "step": at, "history": history, "numpy_rng": rng.bit_generator.state,
            "torch_rng": torch.get_rng_state(), "cuda_rng": torch.cuda.get_rng_state(),
            "recipe_sha256": digest(args.recipe), "replay_manifest_sha256": digest(args.cache/"manifest.json"),
            "deployment_ready": False, "asynchronous_runner": False}
        torch.save(state, path)
        return path

    if step == 0:
        metrics = evaluate(model,target,data,records,cfg)
        history.append({"step":0, **metrics})
        run.log(metrics, step=0)
        save(0)
    resumed_at = step
    while step < max_steps:
        step += 1
        selected = [int(rng.choice(terminals)) if len(terminals) and rng.random()<0.1
                    else int(rng.choice(grouped[rng.choice(groups)])) for _ in range(cfg["batch_size"])]
        ix = torch.tensor(selected,device="cuda")
        obs, nxt = get_obs(data,data["current"][ix]), get_obs(data,data["next"][ix])
        warmup = step <= cfg["warmup_steps"]
        with torch.no_grad():
            next_pi = model._get_ref_chunk(nxt) if warmup else model.sac_forward(nxt)[0]
            next_q = target(model._critic_state(nxt),next_pi).min(-1,keepdim=True).values
            y = td_target(rewards[ix],data["terminated"][ix,None],data["discount"][ix,None],next_q)
        critic_optim.zero_grad(set_to_none=True)
        q = model.sac_q_forward(obs,data["actions"][ix])
        critic_loss = (q-y).square().mean()
        critic_loss.backward()
        critic_grad = torch.nn.utils.clip_grad_norm_(model.q_head.parameters(),cfg["clip_grad"])
        if not torch.isfinite(critic_loss) or not torch.isfinite(critic_grad):
            raise ValueError("Nonfinite critic update")
        critic_optim.step()
        actor_metrics = {}
        if warmup or step % cfg["actor_update_interval"] == 0:
            actor_optim.zero_grad(set_to_none=True)
            loss, actor_metrics = actor_loss(model,obs,q_weight=0.0 if warmup else cfg["q_weight"],
                bc_weight=cfg["bc_weight"], reference_dropout_prob=cfg["reference_dropout_prob"])
            loss.backward()
            actor_grad = torch.nn.utils.clip_grad_norm_(actor_parameters(model),cfg["clip_grad"])
            if not torch.isfinite(loss) or not torch.isfinite(actor_grad):
                raise ValueError("Nonfinite actor update")
            actor_optim.step()
            actor_metrics.update({"actor/loss":loss.detach(),"actor/grad_norm":actor_grad.detach()})
        update_target(target,model.q_head,cfg["tau"])
        metrics = {"critic/loss":critic_loss.item(),"critic/grad_norm":float(critic_grad),
            "train/learning_rate":cfg["learning_rate"],"train/warmup":int(warmup),
            "train/q_weight":0.0 if warmup else cfg["q_weight"],
            "train/steps_per_second":(step-resumed_at)/(time.monotonic()-started),
            "train/gpu_peak_mib":torch.cuda.max_memory_allocated()/1024**2,
            **{k:float(v) for k,v in actor_metrics.items()}}
        if step % cfg["eval_interval"] == 0 or step in (cfg["warmup_steps"],max_steps):
            metrics.update(evaluate(model,target,data,records,cfg))
            history.append({"step":step, **metrics})
            checkpoint=save(step)
            metrics["checkpoint/step"] = step
            print(json.dumps({"step":step,**metrics}),flush=True)
        run.log(metrics,step=step)
    saved = torch.load(checkpoint,map_location="cpu",weights_only=False)
    probe = get_obs(data,data["current"][:8])
    with torch.no_grad():
        before = model.sac_forward(probe,deterministic=True)[0].clone()
        model.load_state_dict(saved["model"],strict=True)
        after = model.sac_forward(probe,deterministic=True)[0]
        assert torch.equal(before,after)
    result={"architecture":counts,"history":history,"last_checkpoint":str(checkpoint),
        "reload_exact":True,"resumed_from_step":resumed_at,"completed_step":step,
        "train_sources":len({r["source_run"] for r in records if r["split"]=="train"}),
        "validation_sources":len({r["source_run"] for r in records if r["split"]=="validation"}),
        "vla_loaded_in_training_process":False,"encoder_loaded_in_training_process":False,
        "deployment_ready":False,"robot_commands":0,"asynchronous_runner":False}
    write_json(args.output/"result.json",result)
    run.summary.update({"completed_step":step,"reload_exact":True,"robot_commands":0,"deployment_ready":False})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("prepare","train"))
    for name in ("recipe","core","output"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--cache",type=Path)
    p.add_argument("--resume",type=Path)
    p.add_argument("--steps",type=int)
    p.add_argument("--run-id",required=True)
    p.add_argument("--job",required=True,choices=("feature-cache","preflight","offline-actor-critic"))
    args=p.parse_args()
    if os.environ.get("CUDA_VISIBLE_DEVICES") not in ("4","5","6","7"):
        raise ValueError("Use an explicitly idle authorized training GPU4-7")
    if args.output.exists() and not args.resume:
        raise ValueError("New runs need a new output directory")
    args.output.mkdir(parents=True,exist_ok=bool(args.resume))
    cfg=json.loads(args.recipe.read_text())
    seed_all(cfg["seed"])
    torch.set_num_threads(4)
    run=start_run(args,cfg)
    try:
        (prepare if args.command=="prepare" else train)(args,cfg,run)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__=="__main__":
    main()
