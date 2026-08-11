# G2 Phone Packaging Demo

## Goal

Build a dual-arm G2 demo that:

1. picks a phone up from a table;
2. hands the phone from one gripper to the other;
3. places the phone into an open retail phone box;
4. picks and aligns the lid;
5. closes the box.

The intended learning stack is PI0.5 plus RLinf. The exact PI0.5 checkpoint,
RL method, observation contract, action representation, and reward design are
deliberately not fixed yet.

## Repository boundaries

- `../../../PI0.5/`: upstream Physical Intelligence OpenPI source.
- `../../../RLinf/`: upstream RLinf source.
- `../../../g2_robot/`: existing G2 robot runtime, kept unchanged for now.
- This directory: task-specific integration, configuration, experiments,
  evaluation, and lightweight orchestration only.

Do not copy upstream source trees into this task directory. Any required G2,
OpenPI, or RLinf changes should remain isolated and reviewable.

## Layout

- `configs/`: task, model, training, deployment, and reward configurations.
- `scripts/`: task-level data, training, evaluation, and demo entry points.
- `experiments/`: experiment definitions and human-readable run notes.
- `eval/`: success criteria, evaluation protocols, and scoring tools.
- `docs/`: task scope, physical setup, and design decisions.
- `artifacts/`: small generated summaries and manifests; large datasets and
  checkpoints should live outside Git.

## Proposed milestones

1. Freeze the physical setup and define measurable success criteria.
2. Collect teleoperation demonstrations for each skill and the full sequence.
3. Fine-tune PI0.5 with supervised learning and validate closed-loop behavior.
4. Add RL post-training only after the supervised policy is safe and repeatable.
5. Integrate the policy with `g2_robot` through a model-independent policy
   interface, then run shadow and guarded real-robot validation.

## Status

Scaffold only. No model checkpoint, robot interface, or powered behavior has
been selected or implemented.
