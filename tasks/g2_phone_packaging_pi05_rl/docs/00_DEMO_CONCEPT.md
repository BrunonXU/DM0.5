# Demo Concept: Dual-Arm Phone Packaging

## Recommendation

The sequence is visually strong and suitable for studying precise manipulation,
but the initial setup should be designed for repeatability rather than maximum
mechanical difficulty.

Use a dimensionally realistic dummy phone with a durable screen and compliant
gripper pads. Present it on a shallow stand, edge support, or with a controlled
overhang so a parallel gripper can establish a safe grasp. A phone lying fully
flush on a table is not a reliable starting condition for gripper-only pickup.

Start with an open bottom tray and a separately staged lid. Preserve the visual
appearance of a retail phone box, while initially allowing modest insertion and
lid-alignment clearance. Tighten tolerances only after the complete sequence is
repeatable.

## Skill decomposition

1. Acquire the staged phone with the first gripper.
2. Move to a fixed, camera-visible handover pose.
3. Transfer the phone to the second gripper without collision or slip.
4. Align and place the phone in the bottom tray.
5. Acquire and align the lid.
6. Seat the lid with bounded force/position and verify closure.

Train and evaluate every skill separately before chaining the complete task.

## Why this is a good PI0.5 + RLT candidate

- PI0.5 can supply visual and semantic priors plus a demonstration-trained
  reference policy.
- RLT can freeze the expensive visual-language feature model and refine a small
  actor-critic policy for the precision-critical phases.
- Handover, insertion, and lid seating have clear progress and success signals,
  making staged rewards and intervention-based collection feasible.

## Initial model candidate

Use the official `pi05_base` checkpoint as the default Stage 1 initialization,
then perform G2-specific supervised fine-tuning before online RL. Keep
`pi05_droid` as an early comparison experiment because it has useful tabletop
manipulation and language-following priors, but do not assume its robot action
contract transfers directly to G2.

The final choice remains open until the G2 observation/action representation,
camera layout, dataset size, and base-vs-DROID pilot results are measured.

## Guardrails for the first version

- Use a dummy phone, not a valuable device.
- Use compliant gripper contact surfaces and conservative force/position limits.
- Fix the box pose during early experiments.
- Make the handover pose explicit and keep it away from the robot body.
- Require shadow and guarded validation before any learned policy controls the
  physical robot.
