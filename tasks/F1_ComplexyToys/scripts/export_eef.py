#!/usr/bin/env python3
"""Audit trainable-only updates and merge the native LoRA into a bound core bundle."""
import argparse
import json
from pathlib import Path
import shutil
import sys

import torch
from safetensors import safe_open
from safetensors.torch import save_file

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.processing import load_contract, sha256
from f1_toys.lora import merge_state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("checkpoint", "dataset", "rlinf-root", "tokenizer", "base", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--lora-manifest", type=Path)
    p.add_argument("--full", action="store_true")
    a = p.parse_args()
    if a.full == bool(a.lora_manifest):
        raise ValueError("Choose exactly one of --full and --lora-manifest")
    a.output.mkdir(parents=True, exist_ok=False)
    checkpoint = a.checkpoint / "model_state_dict/full_weights.pt"
    state = torch.load(checkpoint, map_location="cpu", weights_only=True, mmap=True)
    if not all(k.startswith("model.") for k in state):
        raise ValueError("Expected SFT wrapper state")
    core = {k.removeprefix("model."): v for k, v in state.items()}
    lora = json.loads(a.lora_manifest.read_text()) if a.lora_manifest else None
    changed, frozen_count = {}, 0
    with safe_open(str(a.base / "model.safetensors"), framework="pt") as base:
        if a.full and (set(core) != set(base.keys()) or any("lora_" in k for k in core)):
            raise ValueError("Full model must have exactly the base core keys, without LoRA")
        for name, value in core.items():
            if not torch.isfinite(value).all():
                raise ValueError("Nonfinite weight " + name)
            if name.endswith((".lora_A", ".lora_B")):
                if name.endswith(".lora_B") and not value.any():
                    raise ValueError("LoRA leaf never updated: " + name)
                continue
            original = base.get_tensor(name.replace(".base.", "."))
            difference = (value - original).abs()
            if not a.full and name not in lora["trainable"]:
                if difference.any():
                    raise ValueError("Frozen weight changed: " + name)
                frozen_count += 1
            else:
                changed[name] = float(difference.max())
            if name.startswith("action_out_proj"):
                assert not difference[10:].any(), name
            if name == "action_in_proj.weight":
                assert not difference[:, 10:].any(), name
    if a.full and not any(v > 0 for v in changed.values()):
        raise ValueError("Full checkpoint has no learned updates")
    merged = core if a.full else merge_state(core)
    save_file(merged, str(a.output / "model.safetensors"))
    cfg = load_contract(a.dataset / "contract.json")
    model_cfg = dict(pi05=True, action_dim=32, action_horizon=50, max_token_len=200,
                     paligemma_variant="gemma_2b", action_expert_variant="gemma_300m", dtype="bfloat16", pcd=False,
                     action_loss_mask=[i < 10 for i in range(32)], mask_inactive_action_inputs=True, train_image_augmentation=False)
    for name in ("contract.json", "norm_stats.json"):
        shutil.copyfile(a.dataset / name, a.output / name)
    shutil.copyfile(a.dataset / "manifest.json", a.output / "dataset_manifest.json")
    shutil.copyfile(a.tokenizer, a.output / "paligemma_tokenizer.model")
    if a.lora_manifest:
        shutil.copyfile(a.lora_manifest, a.output / "lora_manifest.json")
    (a.output / "model_config.json").write_text(json.dumps(model_cfg, indent=2) + "\n")
    core_dir = a.rlinf_root / "rlinf/models/embodiment/openpi_rlinf/pi0_model"
    prep = ["processing.py", "tokenizer.py", "eef.py", "native/kinematics.py", "native/ccs_m6_40.MvKDCfg"]
    if cfg.get('input_version'):
        prep.append('inference.py')  # Bind the prepared-pixels entry as well.
    manifest = dict(schema="f1_toys_inference_bundle_v1", action_schema=cfg["schema"],
                    checkpoint=str(checkpoint), checkpoint_sha256=sha256(checkpoint),
                    smoke_only=False, generalization_validated=False, robot_motion_validated=False,
                    frozen_tensors_unchanged=frozen_count, projection_updates=changed,
                    training_mode="full" if a.full else "lora",
                    base_sha256=sha256(a.base / "model.safetensors"),
                    all_lora_B_updated=None if a.full else True, inactive_projections_unchanged=True,
                    files_sha256={f.name: sha256(f) for f in a.output.iterdir() if f.is_file()},
                    core_sha256={str(f.relative_to(core_dir)): sha256(f) for f in core_dir.rglob("*.py")},
                    preprocessing_sha256={n: sha256(TASK / "src/f1_toys" / n) for n in prep})
    (a.output / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(result="PASS", frozen=frozen_count, projections=changed, output=str(a.output))), flush=True)


if __name__ == "__main__":
    main()
