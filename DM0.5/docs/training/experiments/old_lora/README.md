---
doc_type: experiment
domain: training
status: frozen
created: 2026-08-13
updated: 2026-08-24
models: [old_lora]
hardware: [rtx6000d, g2]
canonical_for: [old_lora_training_index]
supersedes: [LoRAExperiments/OldLora/README.md]
superseded_by: []
related: []
---

# OldLora index

The original experiment remains unchanged and is treated as the comparison
baseline:

- action representation: 34D absolute;
- image processing: NoAug;
- model horizon: 50;
- formal run: 8 GPUs, global batch 32;
- selected checkpoint in the previous evaluation: 35K.

Original evidence remains in:

- `FORMAL_50K_TRAINING.md`
- `CHECKPOINT_VALIDATION_AND_SELECTION.md`
- `EXTENSION_50K_TO_100K.md`
- `EIGHT_GPU_500_STEP_GATE.md`
- `g2_sort/training/dm05_lora.py`

No NewLora artifact may be written into an OldLora checkpoint or norm-stats
directory.
