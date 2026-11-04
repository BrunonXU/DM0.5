#!/usr/bin/env python3
"""Export only inference weights and their exact task/core contract on H20."""
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--checkpoint", type=Path, required=True, help="global_step_N/actor")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--rlinf-root", type=Path, required=True)
    p.add_argument("--tokenizer", type=Path, required=True)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    contract = load_contract(args.dataset / "contract.json")
    checkpoint = args.checkpoint / "model_state_dict/full_weights.pt"
    state = torch.load(checkpoint, map_location="cpu", weights_only=True, mmap=True)
    if not state or not all(k.startswith("model.") for k in state):
        raise ValueError("Expected the non-RLT SFT wrapper checkpoint")
    core = {k.removeprefix("model."): v.contiguous() for k, v in state.items()}
    if not all(torch.isfinite(v).all().item() for v in core.values()):
        raise ValueError("Checkpoint contains non-finite weights")
    # The small action projections give direct evidence of an optimizer update.
    changed = {}
    inactive = [i for i in range(32) if i not in contract["active_action_indices"]]
    with safe_open(str(args.base / "model.safetensors"), framework="pt") as base:
        for key in [k for k in core if "action_in_proj" in k or "action_out_proj" in k]:
            delta = (core[key].float() - base.get_tensor(key).float()).abs()
            changed[key] = dict(max_abs_delta=delta.max().item(), changed_values=int((delta > 0).sum()))
            if "action_out_proj" in key:
                assert not delta[inactive].any(), "Unsupervised output rows changed"
            if key == "action_in_proj.weight":
                assert not delta[:, inactive].any(), "Inactive noisy-action input columns changed"
    if not changed or not any(v["changed_values"] for v in changed.values()):
        raise ValueError("No verified action-projection update from the base weights")
    save_file(core, str(args.output / "model.safetensors"))
    for name in ("contract.json", "norm_stats.json", "manifest.json"):
        shutil.copyfile(args.dataset / name, args.output / ("dataset_manifest.json" if name == "manifest.json" else name))
    shutil.copyfile(args.tokenizer, args.output / "paligemma_tokenizer.model")
    config = dict(pi05=True, action_dim=32, action_horizon=contract["horizon"], max_token_len=contract["max_token_len"],
                  paligemma_variant="gemma_2b", action_expert_variant="gemma_300m", dtype="bfloat16", pcd=False,
                  action_loss_mask=[i in contract["active_action_indices"] for i in range(32)],
                  mask_inactive_action_inputs=True, train_image_augmentation=False)
    (args.output / "model_config.json").write_text(json.dumps(config, indent=2) + "\n")
    core_dir = args.rlinf_root / "rlinf/models/embodiment/openpi_rlinf/pi0_model"
    manifest = dict(schema="f1_toys_inference_bundle_v1", checkpoint=str(checkpoint), checkpoint_sha256=sha256(checkpoint),
                    smoke_only=True, formal_training_ready=False, right_projection_updates=changed,
                    inactive_projection_weights_unchanged=True,
                    files_sha256={f.name: sha256(f) for f in args.output.iterdir() if f.is_file()},
                    core_sha256={str(f.relative_to(core_dir)): sha256(f) for f in core_dir.rglob("*.py")},
                    preprocessing_sha256={name: sha256(TASK / "src/f1_toys" / name) for name in ("processing.py", "tokenizer.py")})
    (args.output / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(dict(result="PASS", bundle=str(args.output), tensors=len(core), updates=changed)), flush=True)


if __name__ == "__main__":
    main()
