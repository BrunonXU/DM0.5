#!/usr/bin/env python3
"""Frozen B1250 RLT pilot: feature cache, token training and offline export.

Runs only on saved data. Reuses the pinned RLinf architecture and F1 reader;
never starts a robot/service or changes the parent checkpoint.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import random
import sys
import time

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.processing import sha256
from f1_toys.rlt import (FrozenTokenReader, PrefixCache, extract_prefix,
                         reconstruction_mse, select_anchors, token_class,
                         validate_splits)


def write_json(path, value):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def start_wandb(args, recipe, job):
    import wandb
    cfg = recipe["wandb"]
    run = wandb.init(project=cfg["project"], entity=cfg["entity"],
        group="00_Engineering_Checks" if job == "preflight" else cfg["group"],
        job_type=job, id=args.run_id, name=f"{recipe['experiment']}_{job}",
        resume="must" if getattr(args, "resume", None) else "never",
        dir=str(args.output), mode=os.environ.get("WANDB_MODE", "online"),
        tags=["F1", "RLT", "frozen_B1250", "token_only", "offline_robot_data", job],
        config={"recipe": recipe, "recipe_sha256": sha256(args.recipe),
                "runner_sha256": sha256(Path(__file__)),
                "adapter_sha256": sha256(TASK / "src/f1_toys/rlt.py")})
    write_json(args.output / "wandb.json", {"id": run.id, "url": run.url, "mode": run.settings.mode})
    return run


def load_policy(args, recipe):
    from f1_toys.inference import Policy
    policy = Policy(recipe["parent_bundle"], args.core)
    policy.model.eval().requires_grad_(False)
    return policy


@torch.no_grad()
def fixed_prediction(policy, observation, seed):
    from f1_toys.inference import batched_observation
    obs = batched_observation(observation, policy.observation_type, policy.device)
    with torch.autocast("cuda", dtype=torch.bfloat16):
        return policy.model.sample_actions(obs, num_steps=5,
            rng=torch.Generator(device=policy.device).manual_seed(seed)).float().cpu()


def cache_features(args, recipe, run):
    from f1_toys.dataset import F1Dataset
    bundle = Path(recipe["parent_bundle"])
    for filename, key in (("model.safetensors", "parent_model_sha256"),
                          ("contract.json", "contract_sha256"),
                          ("norm_stats.json", "norm_stats_sha256")):
        if sha256(bundle / filename) != recipe[key]:
            raise ValueError("Parent identity mismatch: " + filename)
    datasets = {s: F1Dataset(Path(recipe["dataset"]) / s, training=False)
                for s in ("train", "validation")}
    validate_splits(datasets["train"], datasets["validation"])
    policy = load_policy(args, recipe)
    if sha256(bundle / "contract.json") != datasets["train"].manifest["contract_sha256"]:
        raise ValueError("Parent/data contract mismatch")
    selections = {s: select_anchors(ds.rows, recipe[f"{s}_anchors_per_episode"])
                  for s, ds in datasets.items()}
    protocol = {"recipe_sha256": sha256(args.recipe), "selection": selections,
                "selection_method": "uniform_temporal_per_episode_no_label_focus",
                "datasets": {s: sha256(ds.root / "manifest.json") for s, ds in datasets.items()}}
    write_json(args.output / "protocol.json", protocol)
    probe = datasets["validation"][selections["validation"][0]][0]
    before = fixed_prediction(policy, probe, recipe["seed"])
    manifest = {"schema": "f1_rlt_prefix_cache_v1", "splits": {},
                "parent_bundle_sha256": policy.bundle_id,
                "parent_model_sha256": recipe["parent_model_sha256"],
                "recipe_sha256": sha256(args.recipe),
                "token_source_sha256": sha256(args.core / "rlinf/models/embodiment/modules/rlt_token_transformer.py"),
                "protocol_sha256": sha256(args.output / "protocol.json"),
                "all_prefix_positions_preserved": True, "augmentation": False}
    count, started = 0, time.perf_counter()
    for split, ds in datasets.items():
        selected = selections[split]
        feature_file = args.output / f"{split}.bf16"
        masks, records, mapped = [], [], None
        for j, index in enumerate(selected):
            observation = ds[index][0]
            prefix, mask, _ = extract_prefix(policy, observation)
            cpu = prefix[0].to(dtype=torch.bfloat16, device="cpu").contiguous()
            if mapped is None:
                shape = (len(selected), *cpu.shape)
                if shape[1] > recipe["token_config"]["prefix_seq_len"]:
                    raise ValueError("Prefix exceeds token position budget")
                mapped = np.memmap(feature_file, mode="w+", dtype=np.uint16, shape=shape)
            mapped[j] = cpu.view(torch.uint16).numpy()
            masks.append(mask[0].cpu().numpy())
            row = ds.rows[index]
            records.append({k: row[k] for k in ("episode_id", "dataset_role", "supervision_phase") if k in row})
            records[-1]["index"] = index
            count += 1
            if count % 32 == 0:
                metrics = {"cache/observations": count, "cache/observations_per_second": count / (time.perf_counter()-started)}
                run.log(metrics, step=count)
                print(json.dumps({"split": split, "completed": j+1, "total": len(selected), **metrics}), flush=True)
        mapped.flush()
        del mapped
        np.save(args.output / f"{split}-masks.npy", np.stack(masks), allow_pickle=False)
        names = (f"{split}.bf16", f"{split}-masks.npy")
        manifest["splits"][split] = {"shape": list(shape), "records": records,
            "files_sha256": {name: sha256(args.output / name) for name in names}}
    after = fixed_prediction(policy, probe, recipe["seed"])
    torch.testing.assert_close(before, after, rtol=0, atol=0)
    assert all(p.grad is None and not p.requires_grad for p in policy.model.parameters())
    assert sha256(bundle / "model.safetensors") == recipe["parent_model_sha256"]
    manifest["frozen_audit"] = {"same_noise_action_max_abs_diff": float((before-after).abs().max()),
                                "vla_gradients": 0, "parent_file_hash_unchanged": True,
                                "robot_commands": 0}
    torch.save({"observation": probe, "reference_actions32": before}, args.output / "reference-probe.pt")
    manifest["reference_probe_sha256"] = sha256(args.output / "reference-probe.pt")
    write_json(args.output / "manifest.json", manifest)
    run.summary.update({"cache_counts": {s: len(x["records"]) for s,x in manifest["splits"].items()},
                        "frozen_audit": manifest["frozen_audit"]})
    print("FEATURE_CACHE_COMPLETE", flush=True)


def sample_indices(records, batch_size, generator, ratios):
    groups = defaultdict(lambda: defaultdict(list))
    for i, record in enumerate(records):
        groups[record["dataset_role"]][record["episode_id"]].append(i)
    selected = []
    for role, fraction in ratios.items():
        count = int(round(batch_size * fraction))
        episodes = sorted(groups[role])
        if not episodes:
            raise ValueError("Missing source: " + role)
        for _ in range(count):
            episode = episodes[int(torch.randint(len(episodes), (), generator=generator))]
            choices = groups[role][episode]
            selected.append(choices[int(torch.randint(len(choices), (), generator=generator))])
    if len(selected) != batch_size:
        raise ValueError("Batch size must express the exact source mixture")
    return selected


@torch.no_grad()
def evaluate(module, cache, micro_batch, *, ablations=False):
    module.eval()
    values, zero_values, shuffled_values, z_values = [], [], [], []
    for start in range(0, len(cache), micro_batch):
        indices = list(range(start, min(start+micro_batch, len(cache))))
        x, mask = cache.batch(indices, "cuda")
        with torch.autocast("cuda", dtype=torch.bfloat16):
            reconstruction, z = module.reconstruct(x, mask)
            if ablations:
                zero_values.extend(reconstruction_mse(module.decode(torch.zeros_like(z), x, mask), x, mask).cpu().tolist())
                # Use a distant observation, never merely another adjacent frame.
                other = [(i+len(cache)//2) % len(cache) for i in indices]
                other_x, other_mask = cache.batch(other, "cuda")
                other_z = module.encode(other_x, other_mask)
                shuffled_values.extend(reconstruction_mse(module.decode(other_z, x, mask), x, mask).cpu().tolist())
        values.extend(reconstruction_mse(reconstruction, x, mask).cpu().tolist())
        z_values.append(z.flatten(1).float().cpu())
    episodes = defaultdict(list)
    for record, value in zip(cache.records, values):
        episodes[record["episode_id"]].append(value)
    z = torch.cat(z_values)
    result = {"mean_mse": float(np.mean(values)),
              "episode_mean_mse": float(np.mean([np.mean(v) for v in episodes.values()])),
              "episodes": {k: float(np.mean(v)) for k,v in episodes.items()},
              "z_std_mean": float(z.std(dim=0).mean()), "z_norm_mean": float(z.norm(dim=1).mean()),
              "observations": len(values)}
    if ablations:
        result.update(zero_token_mse=float(np.mean(zero_values)), shuffled_token_mse=float(np.mean(shuffled_values)))
    return result


@torch.no_grad()
def checkpoint_probe(module, cache):
    module.eval()
    x, mask = cache.batch([0, 1], "cuda")
    with torch.autocast("cuda", dtype=torch.bfloat16):
        return module.encode_flat(x, mask).float().cpu()


def save_checkpoint(path, module, optimizer, generator, step, best, args, recipe, cache):
    state = {"schema": "f1_rlt_training_v1", "step": step, "best": best,
             "module": module.state_dict(), "optimizer": optimizer.state_dict(),
             "sampler_rng": generator.get_state(), "torch_rng": torch.get_rng_state(),
             "cuda_rng": torch.cuda.get_rng_state_all(), "probe": checkpoint_probe(module, cache),
             "recipe_sha256": sha256(args.recipe), "cache_sha256": sha256(args.cache / "manifest.json"),
             "run_id": args.run_id, "token_config": recipe["token_config"]}
    temporary = path.with_suffix(".tmp")
    torch.save(state, temporary)
    temporary.replace(path)


def train(args, recipe, run):
    train_cache = PrefixCache(args.cache, "train")
    val_cache = PrefixCache(args.cache, "validation")
    if train_cache.manifest["recipe_sha256"] != sha256(args.recipe):
        raise ValueError("Cache recipe mismatch")
    module = token_class(args.core)(**recipe["token_config"]).cuda()
    optim = recipe["optimizer"]
    optimizer = torch.optim.AdamW(module.parameters(), lr=optim["lr"], weight_decay=optim["weight_decay"])
    generator = torch.Generator().manual_seed(recipe["seed"])
    step, best = 0, float("inf")
    if args.resume:
        saved = torch.load(args.resume, map_location="cpu", weights_only=True)
        if (saved["recipe_sha256"] != sha256(args.recipe) or saved["cache_sha256"] != sha256(args.cache / "manifest.json")
                or saved["run_id"] != args.run_id):
            raise ValueError("Resume identity mismatch")
        module.load_state_dict(saved["module"], strict=True)
        optimizer.load_state_dict(saved["optimizer"])
        generator.set_state(saved["sampler_rng"])
        torch.set_rng_state(saved["torch_rng"])
        torch.cuda.set_rng_state_all(saved["cuda_rng"])
        step, best = saved["step"], saved["best"]
        torch.testing.assert_close(checkpoint_probe(module, train_cache), saved["probe"], rtol=0, atol=0)
        assert all(int(v["step"].item()) == step for v in optimizer.state.values())
        write_json(args.output / "resume-verification.json", {"from_step": step,
            "optimizer_steps_verified": True, "encoder_probe_exact": True, "sampler_rng_restored": True})
        del saved
    else:
        initial = evaluate(module, val_cache, recipe["micro_batch_size"])
        write_json(args.output / "initial-validation.json", initial)
        run.log({"validation/initial_episode_mse": initial["episode_mean_mse"]}, step=0)
    run.summary["trainable_parameters"] = sum(p.numel() for p in module.parameters())
    run.summary["vla_parameters_in_optimizer"] = 0
    batch_size, micro = recipe["batch_size"], recipe["micro_batch_size"]
    if batch_size % micro:
        raise ValueError("Batch size must divide into micro batches")
    limit = args.steps or recipe["max_steps"]
    stream = (args.output / "metrics.jsonl").open("a")
    while step < limit:
        started = time.perf_counter()
        module.train()
        optimizer.zero_grad(set_to_none=True)
        indices = sample_indices(train_cache.records, batch_size, generator, recipe["sampling"])
        loss_sum = 0.0
        for offset in range(0, batch_size, micro):
            x, mask = train_cache.batch(indices[offset:offset+micro], "cuda")
            with torch.autocast("cuda", dtype=torch.bfloat16):
                loss, _ = module.loss(x, mask)
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite reconstruction loss")
            (loss / (batch_size // micro)).backward()
            loss_sum += float(loss.detach()) / (batch_size // micro)
        norm = torch.nn.utils.clip_grad_norm_(module.parameters(), optim["clip_grad"], error_if_nonfinite=True)
        optimizer.step()
        step += 1
        elapsed = time.perf_counter() - started
        metrics = {"train/loss": loss_sum, "train/grad_norm": float(norm), "train/lr": optim["lr"],
                   "train/observations_per_second": batch_size / elapsed, "train/step_seconds": elapsed,
                   "train/peak_gpu_gib": torch.cuda.max_memory_allocated()/1024**3}
        if step % recipe["eval_interval"] == 0 or step == limit:
            validation = evaluate(module, val_cache, micro)
            write_json(args.output / f"validation-{step:06d}.json", validation)
            metrics["validation/episode_mse"] = validation["episode_mean_mse"]
            metrics["validation/z_std_mean"] = validation["z_std_mean"]
            improved = validation["episode_mean_mse"] < best
            best = min(best, validation["episode_mean_mse"])
            path = args.output / f"step-{step:06d}.pt"
            save_checkpoint(path, module, optimizer, generator, step, best, args, recipe, train_cache)
            pointer = {"step": step, "path": path.name, "validation_episode_mse": validation["episode_mean_mse"]}
            write_json(args.output / "last.json", pointer)
            if improved:
                write_json(args.output / "best.json", pointer)
            metrics["checkpoint/step"] = step
            run.summary["best_validation_episode_mse"] = best
            run.summary["checkpoint_step"] = step
        stream.write(json.dumps({"step": step, **metrics})+"\n")
        stream.flush()
        run.log(metrics, step=step)
        if step <= 3 or step % 10 == 0:
            print(json.dumps({"step": step, **metrics}), flush=True)
    stream.close()
    print("TOKEN_TRAINING_COMPLETE", flush=True)


def export_and_verify(args, recipe, run):
    from safetensors.torch import save_file
    val_cache = PrefixCache(args.cache, "validation")
    best = json.loads((args.training / "best.json").read_text())
    saved = torch.load(args.training / best["path"], map_location="cpu", weights_only=True, mmap=True)
    if saved["recipe_sha256"] != sha256(args.recipe) or saved["cache_sha256"] != sha256(args.cache / "manifest.json"):
        raise ValueError("Export identity mismatch")
    with torch.device("meta"):
        module = token_class(args.core)(**recipe["token_config"])
    module.load_state_dict(saved["module"], strict=True, assign=True)
    module = module.cuda().eval().requires_grad_(False)
    report = evaluate(module, val_cache, recipe["micro_batch_size"], ablations=True)
    initial = json.loads((args.training / "initial-validation.json").read_text())
    report["initial_episode_mean_mse"] = initial["episode_mean_mse"]
    report["selected_step"] = best["step"]
    report["validation_improved"] = report["episode_mean_mse"] < initial["episode_mean_mse"]
    artifact = args.output / "encoder"
    artifact.mkdir()
    save_file({k:v.detach().cpu().contiguous() for k,v in module.encoder.state_dict().items()}, str(artifact / "encoder.safetensors"))
    manifest = {"schema": "f1_rlt_encoder_v1", "token_config": recipe["token_config"],
                "parent_bundle_sha256": val_cache.manifest["parent_bundle_sha256"],
                "parent_model_sha256": recipe["parent_model_sha256"],
                "token_source_sha256": val_cache.manifest["token_source_sha256"],
                "encoder_sha256": sha256(artifact / "encoder.safetensors"),
                "recipe_sha256": sha256(args.recipe), "selected_step": best["step"],
                "online_rl_trained": False, "deployment_ready": False}
    write_json(artifact / "manifest.json", manifest)
    # Retain the checkpoint probe before freeing the large training module.
    x, mask = val_cache.batch([0], "cuda")
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
        expected_z = module.encode_flat(x, mask).float().cpu()
    del x, mask, module, saved
    torch.cuda.empty_cache()
    policy = load_policy(args, recipe)
    reader = FrozenTokenReader(artifact, policy, args.core)
    probe = torch.load(args.cache / "reference-probe.pt", weights_only=False)
    encoded = reader.encode(probe["observation"])
    torch.testing.assert_close(encoded["z_rl"].cpu(), expected_z, rtol=0, atol=0)
    reference = fixed_prediction(policy, probe["observation"], recipe["seed"])
    torch.testing.assert_close(reference, probe["reference_actions32"], rtol=0, atol=0)
    report.update(encoder_reload_exact=True, live_vs_cache_token_exact=True,
                  same_noise_reference_exact=True, robot_commands=0,
                  z_shape=list(encoded["z_rl"].shape), proprio_shape=list(encoded["proprio"].shape),
                  online_rl_trained=False, deployment_ready=False)
    write_json(args.output / "report.json", report)
    run.summary.update(report)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("cache", "train", "export"))
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--training", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job", default="train")
    args = parser.parse_args()
    if os.environ.get("CUDA_VISIBLE_DEVICES") not in ("4", "5", "6", "7"):
        raise ValueError("This pilot requires one explicitly selected H20 training GPU")
    args.output.mkdir(parents=True, exist_ok=bool(args.resume))
    recipe = json.loads(args.recipe.read_text())
    assert recipe["freeze_vla"] and not recipe["augmentation"] and recipe["rlt_use_mask"]
    seed_all(recipe["seed"])
    run = start_wandb(args, recipe, {"cache":"feature-extraction", "export":"offline-evaluation"}.get(args.mode, args.job))
    try:
        {"cache":cache_features, "train":train, "export":export_and_verify}[args.mode](args, recipe, run)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__ == "__main__":
    main()
