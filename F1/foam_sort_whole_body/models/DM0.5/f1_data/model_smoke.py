"""CPU-only real processor/collator smoke; no model weights or training."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pose-cache", type=Path, required=True)
    parser.add_argument("--statistics", type=Path, required=True)
    parser.add_argument("--image-cache-root", type=Path, required=True)
    parser.add_argument("--processor", required=True)
    args = parser.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from transformers import AutoProcessor
    from .state_tokens import make_tokenizer
    from opendm.data.collator import TrainingCollator
    from .reader import Episode, F1Dataset, Normalizer, require
    from .images import ImageLoader

    processor = AutoProcessor.from_pretrained(args.processor, local_files_only=True)
    episode = Episode(args.source, args.pose_cache, allow_candidates=True)
    normalizer = Normalizer(json.loads(args.statistics.read_text()))
    results = {}
    for training in (False, True):
        images = ImageLoader(args.image_cache_root, training=training)
        tokenizer = make_tokenizer(processor, image_keys=["images_1", "images_2", "images_3"], max_length=1024)
        dataset = F1Dataset([episode], normalizer, images, tokenizer)
        samples = [dataset[0], dataset[len(dataset) - 1]]
        require(all(s["input_ids"].shape[1] <= 1024 for s in samples), "token prefix over budget")
        batch = TrainingCollator(processor.tokenizer.pad_token_id)(samples)
        require(batch["action"].shape == (2, 50, 32), "action batch shape mismatch")
        require(batch["action_mask"].dtype == torch.bool, "non-bool action mask")
        require(not batch["action_mask"][..., 23:].any(), "padding supervision")
        require(torch.isfinite(batch["action"]).all(), "nonfinite normalized actions")
        require(all(value.device.type == "cpu" for value in batch.values()), "non-CPU batch")
        results["training_aug" if training else "validation"] = {
            key: {"shape": list(value.shape), "dtype": str(value.dtype), "device": str(value.device)}
            for key, value in batch.items()}
    print(json.dumps({"status": "passed", "training_approved": False, "model_weights_loaded": False,
                      "gpu_used": False, "processor_source": args.processor,
                      "scope": "F1 reader, original images, shared transforms/tokenizer/collator; CPU only",
                      "batches": results}, indent=2))


if __name__ == "__main__":
    main()
