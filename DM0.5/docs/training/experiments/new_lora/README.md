---
doc_type: experiment
domain: training
status: frozen
created: 2026-08-13
updated: 2026-08-24
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [new_lora_training_contract]
supersedes: [LoRAExperiments/NewLora/README.md]
superseded_by: []
related: [../../../core/TRAINING_AND_DEPLOYMENT_CONTRACT.md]
---

# NewLora: G2 mixed-relative + vendor official augmentation LoRA

## Documents

- [Checkpoint验证设计](CHECKPOINT_VALIDATION_DESIGN.md)
- [Validation v1实验报告](VALIDATION_REPORT.md)
- [75K部署与视觉门历史快照](CHECKPOINT_75K_DEPLOYMENT_AND_VISUAL_GATE.md)
- [70K/75K视觉增强配对测试](VISUAL_AUG_PAIRED_REPORT.md)
- RTC、compile与真机实验已移到 [`../../../robot/experiments/`](../../../robot/experiments/)；
  通用运行时原理见 [`../../../robot/`](../../../robot/)，真机事故见
  [`../../../robot/incidents/`](../../../robot/incidents/)。

## Motivation

The OldLora real-robot rollout often reached toward the narrow training-set
location when the bottle was displaced. A failed grasp could still be followed
by handoff and placement motions. Telemetry shows that the complete incorrect
macro sequence crossed several new observations and policy requests, so it
cannot be explained only by replaying one stale action chunk.

The likely combined causes are a narrow object-position distribution, absolute
arm trajectory anchoring, a collection-to-rollout scene and illumination gap,
weak visual influence relative to robot-state phase, and the absence of
failed-grasp recovery demonstrations.

An initial NoAug smoke run reached step 928 and was intentionally stopped on
2026-08-13 after confirming that both the physical scene and illumination
differ between collection and rollout. Its container and logs are retained as
`NewLora-NoAug-aborted-smoke-20260813`; it is not a formal model candidate.

## Frozen training contract

- experiment prefix: `NewLora`;
- dataset: the same frozen 800-episode train/validation split as OldLora;
- base model: the same DM0.5 snapshot as OldLora;
- action tensor: `50 x 34`;
- arm dimensions 0..13: relative to the current 14D arm state;
- hand dimensions 14..33: absolute targets;
- images: `images_head`, `images_left_wrist`, `images_right_wrist`, unchanged;
- image augmentation: vendor-recommended full pipeline, independently applied
  to each camera: pad-to-square, resize 448, 5% random resized crop, rotation
  within +/-2 degrees, and color jitter (brightness 0.3, contrast 0.4,
  saturation 0.3, hue 0.0); crop, rotation and jitter each use `p=0.5`;
- GPUs: physical GPUs 4,5,6,7;
- per-device batch: 4;
- gradient accumulation: 2;
- global batch: 32;
- steps: 75K from step 0;
- checkpoint interval/retention: every 5K, retain 15;
- monitoring: restartable Docker-owned job, tmux log follower, local logs, GPU
  CSV and W&B.

The formal experiment changes both action representation and image
augmentation. Any improvement over OldLora therefore cannot be attributed to
relative actions alone. This combined recipe is intentional: it tests the
vendor recommendation under the observed real scene and lighting domain gap.

## Artifact isolation

- recipe: `g2_sort/training/NewLora_dm05_lora.py`
- checkpoint: `server/checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/`
- norm stats: `server/processed/full/NewLora_officialaug_norm_stats/`
- run logs: `server/runs/NewLora/g2_armrel_handabs_officialaug_4gpu_75k_v1/`
- tmux: `NewLora-officialaug-g2-4gpu-75k-v1`
- Docker: `NewLora-g2-armrel-handabs-officialaug-4gpu-75k-v1`
- W&B project: `NewLora_DM05_G2`
- W&B run ID: `NewLora-g2-armrel-handabs-officialaug-4gpu-75k-v1`

The run must never resume from an OldLora checkpoint. The first launch must
verify that the NewLora output directory contains no checkpoint.

## Actual augmentation examples

The rendered examples use a real frame from training episode
`sorting_20260409161901` and the same augmentation definitions as the formal
run. The source videos are read-only; augmentation is generated in memory each
time a sample is loaded and never overwrites the dataset.

- all three cameras: `augmentation_examples/three_camera_official_aug_examples.jpg`
- per-camera sheets: `augmentation_examples/images_*_official_aug_examples.jpg`
- sampled-operation record: `augmentation_examples/augmentation_replay.json`

## Deployment contract

The model continues to predict 50 actions. The formal powered-sync client
executes 20 of the 49 available 30 Hz intervals before replanning, about 0.667
seconds of open-loop playback. Training horizon remains 50.

Mixed relative inference must inject the exact same state description used by
training and must invert only the first 14 dimensions:

```text
absolute_arm = observed_arm + predicted_arm_delta
absolute_hand = predicted_hand
```

## IMAGE_KEYS decision

The existing image content and ordering are consistent end to end:

1. `images_head`
2. `images_left_wrist`
3. `images_right_wrist`

Canonical `images_1/2/3` names would change the text labels inserted by the
tokenizer, but there is no evidence that the present names swapped or dropped
camera inputs. They remain unchanged in NewLora and can be tested separately.

## Post-training validation plan

The executable NewLora validation implementation is isolated in:

- `g2_sort/validation/newlora_common.py`;
- `g2_sort/validation/evaluate_NewLora_fm.py`;
- `g2_sort/validation/evaluate_NewLora_actions.py`;
- `infra/host/run_NewLora_validation_4gpu.sh`.

Frozen manifests and results are stored under
`server/evaluations/NewLora_validation_v1/`. The formal four-worker run uses
physical GPUs 4--7 and never uses GPU 1 reserved by the running IsaacSim job.

Checkpoint selection must evaluate at least 25K, 35K, 50K and 75K; the last
checkpoint is not assumed to be best.

1. Replay original dataset actions through the robot/control mapping.
2. Run the model on frozen training observations and compare predicted actions
   with demonstrations.
3. Run no-augmentation validation loss and generated-action metrics with the
   mixed-relative output converted back to absolute actions.
4. Test bottle positions inside and outside the collection support.
5. Test failed-grasp observations and whether the next request reaches back.
6. Perform fixed-seed visual counterfactuals: real, black, mismatched and
   temporally shuffled images. Black-image actions are shadow-only.
7. Compare powered-sync 20/50 against the OldLora 49/50 baseline.
8. Only after the policy behavior is understood, integrate RTC and compare it
   separately against powered-sync 20/50.

Future product reliability work may add an object-held gate and explicit
recovery demonstrations. Those are separate from this pure NewLora comparison.
