#!/usr/bin/env python3
"""Fixed in-trajectory diagnostics; never labels overlapping windows a test split."""
import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np
import torch
from safetensors.torch import load_file
from scipy.spatial.transform import Rotation

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.dataset import F1Dataset
from f1_toys.eef import physical_actions, relative_pose, decode_prefix, solver
from f1_toys.inference import load_core, batched_observation
from f1_toys.lora import inject, merge_state
from f1_toys.processing import sha256


def errors(physical, truth, count):
    errors = [np.rad2deg(Rotation.from_matrix(relative_pose(a)[:3,:3].T @ relative_pose(b)[:3,:3]).magnitude())
              for a, b in zip(physical[:count], truth[:count])]
    return dict(position_mm=float(np.linalg.norm(physical[:count,:3]-truth[:count,:3],axis=1).mean()*1000),
                rotation_deg=float(np.mean(errors)), gripper_points=float(np.abs(physical[:count,9]-truth[:count,9]).mean()*50))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("dataset", "rlinf-root", "output"):
        p.add_argument("--"+key, type=Path, required=True)
    p.add_argument("--weights", type=Path, required=True, help="base/merged safetensors or actor/full_weights.pt")
    p.add_argument("--adapter", action="store_true")
    p.add_argument("--merge-parity", action="store_true")
    p.add_argument("--indices", default="0,75,150,300,450,550,660,780,909")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    ds = F1Dataset(a.dataset)
    cfg_mod, model_mod = load_core(a.rlinf_root)
    cfg = cfg_mod.Pi0Config(pi05=True, action_dim=32, action_horizon=50, max_token_len=200,
        action_loss_mask=tuple(i < 10 for i in range(32)), mask_inactive_action_inputs=True, train_image_augmentation=False)
    with torch.device("meta"):
        model = cfg.create()
        if a.adapter:
            inject(model)
    is_checkpoint = a.weights.suffix == ".pt"
    state = torch.load(a.weights, map_location="cpu", weights_only=True, mmap=True) if is_checkpoint else load_file(str(a.weights))
    if is_checkpoint:
        if not all(k.startswith("model.") for k in state):
            raise ValueError("Expected SFT wrapper checkpoint")
        state = {k.removeprefix("model."): v for k,v in state.items()}
    model.load_state_dict(state, strict=True, assign=True)
    model = model.cuda().eval()
    ik = solver()
    indices = [int(x) for x in a.indices.split(",")]
    with np.load(a.dataset / "arrays.npz") as arrays:
        held = arrays["submitted"][:,0].copy()
    results, predictions = [], []
    with torch.inference_mode():
        for index in indices:
            data, label = ds[index][:2]
            valid_steps = int(ds.action_time_mask[index].sum())
            obs = batched_observation(data, model_mod.Observation, "cuda")
            baseline = np.zeros((50,10)); baseline[:,3] = baseline[:,7] = 1
            baseline[:,9] = (ds.states[index,15]-50)/50
            for steps in (10,5):
                for seed in (20261016,20261017):
                    begin = time.perf_counter()
                    with torch.autocast("cuda", dtype=torch.bfloat16):
                        pred = model.sample_actions(obs, num_steps=steps, rng=torch.Generator(device="cuda").manual_seed(seed))
                    raw = pred[0].float().cpu().numpy()
                    latency = (time.perf_counter()-begin)*1000
                    assert not raw[:,10:].any() and np.isfinite(raw).all()
                    physical = physical_actions(raw, ds.stats["action"])
                    decoded = decode_prefix(ik, physical, ds.states[index], held[index], 20)
                    row = dict(index=index, episode_id=ds.rows[index]["episode_id"],
                        state_clipping=ds.processor.last_state_diagnostics,
                        seed=seed, denoise_steps=steps, model_ms=latency,
                        valid_action_steps=valid_steps,
                        prefix20=errors(physical, ds.actions[index], min(20, valid_steps)),
                        full50=errors(physical, ds.actions[index],50) if valid_steps == 50 else None,
                        valid_future=errors(physical, ds.actions[index],valid_steps),
                        hold20=errors(baseline,ds.actions[index],min(20, valid_steps)), ik_success=decoded["success"],
                        ik_ms=sum(d["latency_ms"] for d in decoded["diagnostics"]),
                        max_joint_step_deg=max(d["reference_step_max_deg"] for d in decoded["diagnostics"]),
                        ik_details=decoded["diagnostics"])
                    results.append(row); predictions.append(raw)
                    print(json.dumps({k:v for k,v in row.items() if k != "ik_details"}), flush=True)
        parity = None
        if a.merge_parity:
            if not a.adapter:
                raise ValueError("Merge parity requires adapter checkpoint")
            # fp32 isolates merge algebra from bf16 GEMM rounding changes.
            model.embed_dtype = model.llm.embed_dtype = model.img.dtype_mm = torch.float32
            obs = batched_observation(ds[indices[0]][0], model_mod.Observation, "cuda")
            reference = model.sample_actions(obs,num_steps=10,rng=torch.Generator(device="cuda").manual_seed(42)).float().cpu()
            merged_state = merge_state(state)
            del model
            torch.cuda.empty_cache()
            with torch.device("meta"):
                merged = cfg.create()
            merged.load_state_dict(merged_state,strict=True,assign=True)
            merged = merged.cuda().eval()
            merged.embed_dtype = merged.llm.embed_dtype = merged.img.dtype_mm = torch.float32
            actual = merged.sample_actions(obs,num_steps=10,rng=torch.Generator(device="cuda").manual_seed(42)).float().cpu()
            error = (reference-actual).abs()
            parity = dict(max_abs=float(error.max()), mean_abs=float(error.mean()), dtype="fp32", same_seed=42)
            torch.testing.assert_close(reference,actual,rtol=1e-4,atol=1e-4)
    summary = {}
    for steps in (10,5):
        selected = [r for r in results if r["denoise_steps"] == steps]
        summary[str(steps)] = dict(samples=len(selected),ik_success_rate=float(np.mean([r["ik_success"] for r in selected])),
            prefix20={k:float(np.mean([r["prefix20"][k] for r in selected])) for k in ("position_mm","rotation_deg","gripper_points")},
            hold20={k:float(np.mean([r["hold20"][k] for r in selected])) for k in ("position_mm","rotation_deg","gripper_points")},
            model_ms_p50=float(np.median([r["model_ms"] for r in selected][1:])))
    np.savez_compressed(a.output/"predictions.npz",predictions=np.stack(predictions))
    report = dict(scope=("held-out episodes, same object/task; not unseen-object generalization"
                         if ds.manifest.get("split") == "validation" else "in-trajectory diagnostic, not independent generalization"),
                  dataset_manifest_sha256=sha256(a.dataset / "manifest.json"), weights=str(a.weights),
                  weights_sha256=sha256(a.weights),summary=summary,merge_parity=parity,results=results)
    (a.output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(dict(summary=summary,merge_parity=parity)),flush=True)


if __name__ == "__main__":
    main()
