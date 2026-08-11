# Embodied AI Robotics Workspace

This repository is the top-level workspace for embodied-AI research and robot
demos. DM0.5 is one policy stack within the workspace; it is not the repository
boundary or the long-term project identity.

## Components

| Path | Responsibility |
| --- | --- |
| `DM0.5/` | DM0.5 training, validation, inference server, and deployment tooling |
| `PI0.5/` | Official Physical Intelligence OpenPI source, tracked as an upstream submodule |
| `RLinf/` | Official RLinf source, tracked as an upstream submodule |
| `g2_robot/` | G2 robot-side cameras, state, safety, control, operations, and policy runtime |
| `tasks/` | Task-level integration, configs, experiments, and evaluation |

## Current demo

The first PI0.5 + RLinf task is a dual-arm phone-packaging demo:

1. pick up a staged dummy phone;
2. hand it from one gripper to the other;
3. place it into a phone box;
4. align and close the lid.

See [`tasks/g2_phone_packaging_pi05_rl/`](tasks/g2_phone_packaging_pi05_rl/)
for the task scope and layout.

## Architecture

The workspace separates reusable capabilities from task integration:

```text
task definition and evaluation
        |       |
        |       +---- RLinf post-training
        +------------ DM0.5 / PI0.5 policy
                         |
                    policy contract
                         |
                     g2_robot
                         |
                        G2
```

Model repositories should not own robot safety logic. Robot-side code should
consume an explicit observation/action contract instead of importing a model's
training internals.

## Repository policy

Source code, small configurations, documentation, tests, and reproducibility
manifests belong in Git. The following do not:

- datasets and converted episodes;
- model weights and checkpoints;
- robot and training logs;
- generated artifacts, videos, and evaluation outputs;
- deployment archives and local caches;
- credentials or machine-local environment files.

Large objects should be stored in the appropriate dataset, model, or artifact
store and referenced by immutable identifiers from the repository.

## Upstream dependencies

OpenPI and RLinf retain their official upstream histories. After cloning this
workspace, initialize them with:

```bash
git submodule update --init --recursive
```

Task-specific integration belongs under `tasks/`; do not copy either upstream
source tree into a task directory.
