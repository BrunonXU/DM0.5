"""Audit the explicitly requested Full SFT parameter scope before FSDP wraps it."""
import json
import os
from pathlib import Path


def audit_full(model):
    rows = [{"name": n, "shape": list(p.shape), "numel": p.numel(),
             "trainable": p.requires_grad, "dtype": str(p.dtype)}
            for n, p in model.named_parameters()]
    if not rows or any(not r["trainable"] or "lora_" in r["name"] for r in rows):
        raise ValueError("Full SFT requires all base parameters trainable and no LoRA")
    report = dict(training_mode="full", total_parameters=sum(r["numel"] for r in rows),
                  trainable_parameters=sum(r["numel"] for r in rows), parameters=rows)
    root = Path(os.environ["F1_TOYS_RUN"])
    root.mkdir(parents=True, exist_ok=True)
    (root / f"full-parameters-{os.getpid()}.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
