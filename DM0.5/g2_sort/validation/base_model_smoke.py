"""Run a real multimodal DM0.5 forward/backward smoke test."""

import argparse
import math
import time

import numpy as np
import torch
from PIL import Image
from transformers import AutoProcessor

from g2_sort.training.dm05_lora import ACTION_DIM, IMAGE_KEYS, DM05ModelConfig
from opendm.data.collator import TrainingCollator
from opendm.data.transforms import ChatTokenization


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--backward", action="store_true")
    parser.add_argument(
        "--attention",
        choices=("official", "server_compat", "fallback"),
        default="official",
    )
    return parser.parse_args()


def build_batch(processor, device: torch.device) -> dict[str, torch.Tensor]:
    image = Image.new("RGB", (448, 448), color=(127, 127, 127))
    sample = {
        "prompt": "sort the beverage bottle into the target bin",
        "state": np.zeros(ACTION_DIM, dtype=np.float32),
        "action": np.zeros((1, 50, ACTION_DIM), dtype=np.float32),
        "action_mask": np.ones((1, 50, ACTION_DIM), dtype=np.bool_),
        "meta_data": {
            "robot_type": None,
            "control_mode": "absolute joint position",
            "speed": "0.5",
        },
        **{key: image.copy() for key in IMAGE_KEYS},
    }
    transform = ChatTokenization(
        processor=processor,
        image_keys=IMAGE_KEYS,
        add_state=True,
        max_length=1024,
    )
    encoded = transform(sample)
    collator = TrainingCollator(
        pad_token_id=processor.tokenizer.pad_token_id,
        max_length=1024,
    )
    return {key: value.to(device) for key, value in collator([encoded]).items()}


def main() -> None:
    args = parse_args()
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("This smoke test requires a CUDA GPU")

    attention_options = {
        "official": {
            "llm_attn_implementation": "flex_attention",
            "vision_attn_implementation": "flash_attention_2",
            "action_attn_implementation": "sdpa",
        },
        "server_compat": {
            "llm_attn_implementation": "sdpa",
            "vision_attn_implementation": "flash_attention_2",
            "action_attn_implementation": "sdpa",
        },
        "fallback": {
            "llm_attn_implementation": "eager",
            "vision_attn_implementation": "sdpa",
            "action_attn_implementation": "sdpa",
        },
    }
    attention = attention_options[args.attention]
    config = DM05ModelConfig(
        model_name_or_path=args.model,
        liger_kernel=False,
        vlm_gradient_checkpointing=args.backward,
        ae_gradient_checkpointing=args.backward,
        **attention,
    )
    processor = AutoProcessor.from_pretrained(args.model, local_files_only=True)
    model = config.build_model(use_lora=args.backward).to(device)
    model.train(args.backward)
    batch = build_batch(processor, device)

    torch.cuda.reset_peak_memory_stats(device)
    torch.cuda.synchronize(device)
    started = time.perf_counter()
    with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
        output = model(**batch)
    if not math.isfinite(float(output.loss.detach())):
        raise RuntimeError(f"Non-finite loss: {output.loss}")
    if args.backward:
        output.loss.backward()
    torch.cuda.synchronize(device)

    trainable = sum(
        parameter.numel()
        for parameter in model.parameters()
        if parameter.requires_grad
    )
    gradients = sum(
        parameter.grad is not None
        for parameter in model.parameters()
        if parameter.requires_grad
    )
    print(f"SMOKE_OK attention={args.attention} backward={args.backward}")
    print(f"loss={float(output.loss.detach()):.6f}")
    print(f"elapsed_s={time.perf_counter() - started:.3f}")
    print(f"peak_gpu_gib={torch.cuda.max_memory_allocated(device) / 2**30:.3f}")
    print(f"trainable_params={trainable} trainable_tensors_with_grad={gradients}")


if __name__ == "__main__":
    main()
