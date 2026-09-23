---
doc_type: archive
domain: training
status: superseded
created: 2026-08-13
updated: 2026-08-24
models: [old_lora, new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: [../../experiments/README.md]
related: []
---

# G2 DM0.5 LoRA experiments（旧索引）

This directory separates the two G2 LoRA recipes without moving or rewriting
their original evidence.

- `OldLora/`: the original 34D absolute-action, no-augmentation experiment.
- `NewLora/`: the isolated 14D arm-relative plus 20D hand-absolute experiment
  with the vendor-recommended full image augmentation pipeline.

Checkpoint, norm-stat, run-log, tmux, Docker and W&B names must use the same
experiment prefix. Cross-experiment resume is forbidden.
