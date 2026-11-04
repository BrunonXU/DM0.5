#!/usr/bin/env python3
"""One synthetic Pi0.5 SFT backward on one explicitly selected idle H20 GPU.

No Ray service, robot, dataset, or full-model checkpoint is started/written.
This is an infra smoke, not F1 data/normalization or distributed-training proof.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def gpu_snapshot(uuid: str) -> dict:
    fields = ["uuid", "memory.total", "memory.free", "utilization.gpu"]
    output = subprocess.check_output(
        ["nvidia-smi", "-i", uuid, "--query-gpu=" + ",".join(fields),
         "--format=csv,noheader,nounits"], text=True, timeout=15,
    ).strip().splitlines()
    if len(output) != 1:
        raise RuntimeError("Expected exactly one GPU")
    values = [s.strip() for s in output[0].split(",")]
    if values[0] != uuid:
        raise RuntimeError("Requested GPU UUID did not match nvidia-smi")
    processes = subprocess.check_output(
        ["nvidia-smi", "-i", uuid, "--query-compute-apps=pid",
         "--format=csv,noheader,nounits"], text=True, timeout=15,
    ).strip().splitlines()
    return dict(uuid=uuid, total_mib=int(values[1]), free_mib=int(values[2]),
                utilization_percent=int(values[3]), compute_processes=processes)


def require_idle(snapshot: dict, minimum_free_gib: float) -> None:
    if snapshot["compute_processes"]:
        raise RuntimeError("Selected GPU already has a compute process; choose an idle GPU")
    if snapshot["free_mib"] < minimum_free_gib * 1024:
        raise RuntimeError("Selected GPU does not meet the free-memory threshold")
    if snapshot["utilization_percent"] > 5:
        raise RuntimeError("Selected GPU is busy")


def run(args, report: dict) -> None:
    # Restrict visibility before importing torch or any RLinf module.
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_uuid
    os.environ["JAX_PLATFORMS"] = "cpu"
    os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("MKL_NUM_THREADS", "4")
    os.environ["WANDB_MODE"] = "offline"
    report["gpu_before_hash"] = gpu_snapshot(args.gpu_uuid)
    require_idle(report["gpu_before_hash"], args.minimum_free_gib)

    manifest_path = args.model / "CONVERSION_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    for filename in ("model.safetensors", "config.json"):
        record = manifest["files"][filename]
        path = args.model / filename
        if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise RuntimeError(f"Converted base checksum mismatch: {filename}")
    config = json.loads((args.model / "config.json").read_text())
    expected = dict(action_dim=32, action_horizon=50, max_token_len=200,
                    paligemma_variant="gemma_2b", action_expert_variant="gemma_300m",
                    pi05=True, pcd=False, dtype="bfloat16")
    if config != expected:
        raise RuntimeError(f"Unexpected base configuration: {config}")
    report["model_manifest_sha256"] = digest(manifest_path)
    report["model_weights_sha256"] = manifest["files"]["model.safetensors"]["sha256"]
    report["model_config"] = config
    report["gpu_immediately_before_import"] = gpu_snapshot(args.gpu_uuid)
    require_idle(report["gpu_immediately_before_import"], args.minimum_free_gib)

    sys.path.insert(0, str(args.rlinf_root.resolve()))
    import torch
    from safetensors.torch import load_file
    from rlinf.models.embodiment.openpi_rlinf.pi0_model.model import Observation
    from rlinf.models.embodiment.openpi_rlinf.pi0_model.pi0_config import Pi0Config
    from rlinf.models.embodiment.openpi_rlinf.sft_action_model import OpenPiPytorchSFTActionModel

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("Exactly one visible CUDA GPU is required")
    torch.set_num_threads(4)
    torch.manual_seed(20261015)
    torch.cuda.manual_seed_all(20261015)  # Only the one explicitly visible GPU.
    torch.cuda.set_device(0)
    total_bytes = torch.cuda.get_device_properties(0).total_memory
    budget_bytes = args.memory_budget_gib * 1024 ** 3
    if budget_bytes >= total_bytes:
        raise RuntimeError("Memory budget must leave GPU headroom")
    torch.cuda.set_per_process_memory_fraction(budget_bytes / total_bytes, 0)
    torch.cuda.reset_peak_memory_stats()
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("Selected GPU does not support bf16")
    print("Loading checksum-verified official base; one visible GPU", flush=True)
    with torch.device("meta"):
        model = Pi0Config(**config).create()
    weights = load_file(str(args.model / "model.safetensors"), device="cpu")
    model.load_state_dict(weights, strict=True, assign=True)
    del weights
    model = model.to(device="cuda", dtype=torch.float32)
    model.gradient_checkpointing_enable()
    wrapper = OpenPiPytorchSFTActionModel(model, num_steps=5, action_env_dim=16)
    wrapper.train()

    # Distinct deterministic images, synthetic token IDs, and zero state/actions.
    # This bypasses the real tokenizer/F1 adapter deliberately and records it.
    image = torch.linspace(-0.8, 0.8, 224 * 224 * 3, device="cuda").reshape(1, 224, 224, 3)
    prompt = torch.zeros((1, 200), dtype=torch.long, device="cuda")
    prompt[:, :8] = torch.tensor([2, 100, 101, 102, 103, 104, 105, 1], device="cuda")
    prompt_mask = torch.zeros((1, 200), dtype=torch.bool, device="cuda")
    prompt_mask[:, :8] = True
    observation = Observation(
        images={"base_0_rgb": image, "left_wrist_0_rgb": torch.zeros_like(image),
                "right_wrist_0_rgb": image.flip(2)},
        image_masks={"base_0_rgb": torch.ones(1, dtype=torch.bool, device="cuda"),
                     "left_wrist_0_rgb": torch.zeros(1, dtype=torch.bool, device="cuda"),
                     "right_wrist_0_rgb": torch.ones(1, dtype=torch.bool, device="cuda")},
        state=torch.zeros((1, 32), dtype=torch.float32, device="cuda"),
        tokenized_prompt=prompt, tokenized_prompt_mask=prompt_mask,
    )
    actions = torch.zeros((1, 50, 32), dtype=torch.float32, device="cuda")
    step_start = time.monotonic()
    print("Synthetic SFT loss + full autograd backward, batch=1 H=50", flush=True)
    with torch.autocast("cuda", dtype=torch.bfloat16):
        loss = wrapper.sft_forward((observation, actions))
    if loss.ndim != 0 or not bool(torch.isfinite(loss)):
        raise RuntimeError("Non-finite/non-scalar synthetic SFT loss")
    loss.backward()
    torch.cuda.synchronize()
    gradient_tensors = 0
    gradient_elements = 0
    gradient_nonzero_tensors = 0
    gradient_max_abs = 0.0
    for name, parameter in model.named_parameters():
        if parameter.grad is None:
            continue
        gradient = parameter.grad
        if not bool(torch.isfinite(gradient).all()):
            raise RuntimeError(f"Non-finite gradient: {name}")
        maximum = float(gradient.abs().max())
        gradient_max_abs = max(gradient_max_abs, maximum)
        gradient_tensors += 1
        gradient_elements += gradient.numel()
        gradient_nonzero_tensors += maximum > 0
    if gradient_tensors < 100 or gradient_nonzero_tensors < 100:
        raise RuntimeError("Too few nonzero parameter gradients for full-model smoke")

    # Test a real, small optimizer update and checkpoint round-trip on the head.
    # All model gradients above were computed, but only this head is updated.
    head = model.action_out_proj
    optimizer = torch.optim.SGD(head.parameters(), lr=1e-4)
    old_weight = head.weight.detach().clone()
    torch.nn.utils.clip_grad_norm_(head.parameters(), max_norm=1.0, error_if_nonfinite=True)
    optimizer.step()
    changed = bool(torch.any(head.weight.detach() != old_weight))
    if not changed:
        raise RuntimeError("Action output head did not update")
    with tempfile.TemporaryDirectory(prefix="pi05-smoke-", dir=args.output.parent) as work:
        checkpoint = Path(work) / "head_and_optimizer.pt"
        torch.save({"head": {name: value.detach().cpu() for name, value in head.state_dict().items()},
                    "optimizer": optimizer.state_dict(), "step": 1}, checkpoint)
        saved_size = checkpoint.stat().st_size
        saved_hash = digest(checkpoint)
        restored = torch.load(checkpoint, map_location="cpu", weights_only=True)
        recovered = torch.nn.Linear(1024, 32, dtype=torch.float32)
        recovered.load_state_dict(restored["head"], strict=True)
        recovered_optimizer = torch.optim.SGD(recovered.parameters(), lr=1.0)
        recovered_optimizer.load_state_dict(restored["optimizer"])
        for name, value in head.state_dict().items():
            if not torch.equal(value.detach().cpu(), recovered.state_dict()[name]):
                raise RuntimeError(f"Head checkpoint round-trip mismatch: {name}")
        if recovered_optimizer.param_groups[0]["lr"] != 1e-4 or restored["step"] != 1:
            raise RuntimeError("Optimizer/step round-trip mismatch")
    peak_allocated = torch.cuda.max_memory_allocated()
    peak_reserved = torch.cuda.max_memory_reserved()
    report.update(
        result="PASS", torch_version=torch.__version__, cuda_runtime=torch.version.cuda,
        gpu_name=torch.cuda.get_device_name(0), model_parameter_count=sum(p.numel() for p in model.parameters()),
        loss=float(loss.detach().float()), gradient_tensors=gradient_tensors,
        gradient_elements=gradient_elements, gradient_nonzero_tensors=gradient_nonzero_tensors,
        gradient_max_abs=gradient_max_abs, head_updated=changed,
        checkpoint_scope="action_out_proj and its SGD optimizer only; temporary file removed",
        checkpoint_bytes=saved_size, checkpoint_sha256=saved_hash,
        checkpoint_roundtrip=True, peak_allocated_gib=peak_allocated / 1024 ** 3,
        peak_reserved_gib=peak_reserved / 1024 ** 3,
        forward_backward_and_check_seconds=time.monotonic() - step_start,
        training_ready=False, f1_data_validated=False, distributed_training_validated=False,
        limitations=["synthetic token IDs/images/state/action; no F1 adapter or real tokenizer",
                     "FP32 masters with bf16 autocast; no FSDP distributed runtime",
                     "full autograd gradients checked, optimizer update/checkpoint only on small output head"],
    )
    if peak_allocated > budget_bytes:
        raise RuntimeError("Observed PyTorch allocation exceeded the smoke budget")
    optimizer.zero_grad(set_to_none=True)
    model.zero_grad(set_to_none=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rlinf-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--gpu-uuid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-free-gib", type=float, default=110.0)
    parser.add_argument("--memory-budget-gib", type=float, default=100.0)
    args = parser.parse_args()
    if not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", args.gpu_uuid):
        parser.error("Specify the full GPU UUID reported by nvidia-smi")
    if not 30 <= args.memory_budget_gib <= 100 or args.minimum_free_gib < args.memory_budget_gib + 10:
        parser.error("Budget must be 30..100 GiB, free threshold at least budget+10 GiB")
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("Output must be a new file in an existing personal directory")
    report = dict(result="RUNNING", created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  script_sha256=digest(Path(__file__)), model=str(args.model), gpu_uuid=args.gpu_uuid,
                  memory_budget_gib=args.memory_budget_gib, minimum_free_gib=args.minimum_free_gib)
    try:
        run(args, report)
    except Exception as exc:
        report.update(result="FAIL", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        report["finished_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with args.output.open("x") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
