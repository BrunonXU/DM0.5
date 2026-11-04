"""Explicit native RLinf Linear-leaf LoRA; no silent PEFT target matching."""
import json
import math
import os
from pathlib import Path
import re

import torch
from torch import nn
from torch.nn import functional as F


class LoRALinear(nn.Module):
    def __init__(self, base, rank=32, alpha=16):
        super().__init__()
        self.base = base
        self.scaling = alpha / rank
        self.lora_A = nn.Parameter(torch.empty(rank, base.in_features, device=base.weight.device, dtype=base.weight.dtype))
        self.lora_B = nn.Parameter(torch.zeros(base.out_features, rank, device=base.weight.device, dtype=base.weight.dtype))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))

    @property
    def weight(self):
        return self.base.weight

    @property
    def bias(self):
        return self.base.bias

    def forward(self, value):
        return self.base(value) + F.linear(F.linear(value, self.lora_A), self.lora_B) * self.scaling


def inject(model, rank=32, alpha=16):
    model.requires_grad_(False)
    targets = []
    # Final prefix outputs aren't consumed by the action loss. Its K/V still
    # condition suffix attention; final expert-0 Q/O have no supervised path.
    final_layer = len(model.llm.layers) - 1
    excluded = {f"llm.layers.{final_layer}.attn.{projection}.0" for projection in ("q_proj", "o_proj")}
    for name, module in list(model.named_modules()):
        attention = re.search(r"\.(q_proj|k_proj|v_proj|o_proj)\.[01]$", name)
        vision = name.startswith("img.") and name.endswith((".fc1", ".fc2"))
        if isinstance(module, nn.Linear) and (attention or vision) and name not in excluded:
            parent_name, leaf = name.rsplit(".", 1)
            setattr(model.get_submodule(parent_name), leaf, LoRALinear(module, rank, alpha))
            targets.append(name)
    if not targets or not any(t.startswith("img.") for t in targets):
        raise ValueError("Native LoRA matched no attention or vision leaves")
    for name, param in model.named_parameters():
        if name.startswith(("action_in_proj.", "action_out_proj.", "time_mlp_in.", "time_mlp_out.")):
            param.requires_grad_(True)
    trainable = {n: dict(shape=list(p.shape), dtype=str(p.dtype), count=p.numel())
                 for n, p in model.named_parameters() if p.requires_grad}
    report = dict(rank=rank, alpha=alpha, dropout=0, modules=targets, trainable=trainable,
                  excluded_no_action_loss_path=sorted(excluded),
                  trainable_parameters=sum(p.numel() for p in model.parameters() if p.requires_grad),
                  total_parameters=sum(p.numel() for p in model.parameters()))
    run = os.environ.get("F1_TOYS_RUN")
    if run:
        Path(run).mkdir(parents=True, exist_ok=True)
        (Path(run) / "lora_manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def merge_state(state, scaling=.5):
    result = {}
    for name, tensor in state.items():
        if name.endswith((".lora_A", ".lora_B")):
            continue
        if ".base." in name:
            module, suffix = name.rsplit(".base.", 1)
            if suffix == "weight":
                tensor = tensor.float() + scaling * (state[module + ".lora_B"].float() @ state[module + ".lora_A"].float())
            name = module + "." + suffix
        result[name] = tensor.contiguous()
    return result
