"""Offline A/B for one Sync/RTC-shared state-continuous hand planner.

Both candidates use :class:`CausalHandTrajectoryPlanner`.  The point-to-point
baseline exposes only the current target, while preview-QP exposes the future
10 Hz reference already present in the accepted policy chunk.  Neither code
path reads scheduler mode, imports GDK, or sends hardware commands.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from g2_sort.deployment.g2_client.trajectory_smoothing import (
    HAND_DIM,
    CausalHandTrajectoryPlanner,
    HandTrajectoryConfig,
)
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_LIMITS
from g2_sort.validation.evaluate_hand_v11_sweep import (
    Candidate as LegacyCandidate,
    HandTraceChunk,
    load_trace,
    metrics,
    replay as replay_legacy,
)


@dataclass(frozen=True)
class PlannerCandidate:
    name: str
    strategy: str
    max_jerk_rad_s3: float
    acceleration_penalty: float
    jerk_penalty: float
    preview_decay: float
    max_velocity_rad_s: float = 1.75
    max_acceleration_rad_s2: float = 7.5
    horizon_steps: int = 6
    enforce_derivative_limits: bool = True

    def config(self) -> HandTrajectoryConfig:
        return HandTrajectoryConfig(
            strategy=self.strategy,
            nominal_hz=10.0,
            horizon_steps=self.horizon_steps,
            max_velocity_rad_s=self.max_velocity_rad_s,
            max_acceleration_rad_s2=self.max_acceleration_rad_s2,
            max_jerk_rad_s3=self.max_jerk_rad_s3,
            acceleration_penalty=self.acceleration_penalty,
            jerk_penalty=self.jerk_penalty,
            preview_decay=self.preview_decay,
            admm_tolerance=5e-4,
            enforce_derivative_limits=self.enforce_derivative_limits,
            position_lower_rad=tuple(POLICY_JOINT_LIMITS[14:, 0]),
            position_upper_rad=tuple(POLICY_JOINT_LIMITS[14:, 1]),
        )


@dataclass
class PlannerReplay:
    commands: list[np.ndarray]
    anchors: list[np.ndarray | None]
    wait_commands: list[list[np.ndarray]]
    converged_steps: int = 0
    nonconverged_steps: int = 0
    discontinuity_resets: int = 0


def default_candidates() -> list[PlannerCandidate]:
    result = [
        PlannerCandidate(
            name=f"p2p_j{jerk:g}",
            strategy="jerk_limited_p2p",
            max_jerk_rad_s3=jerk,
            acceleration_penalty=0.02,
            jerk_penalty=0.05,
            preview_decay=0.35,
        )
        for jerk in (30.0, 50.0, 80.0)
    ]
    for jerk in (30.0, 50.0, 80.0):
        for penalty in (0.02, 0.05, 0.10):
            for decay in (0.2, 0.5):
                result.append(
                    PlannerCandidate(
                        name=(
                            f"preview_j{jerk:g}_jp{penalty:g}_d{decay:g}"
                        ),
                        strategy="preview_qp",
                        max_jerk_rad_s3=jerk,
                        acceleration_penalty=0.02,
                        jerk_penalty=penalty,
                        preview_decay=decay,
                    )
                )
    for acceleration_penalty in (0.0, 0.02):
        for jerk_penalty in (0.05, 0.10, 0.20, 0.50, 1.0, 2.0, 5.0, 10.0):
            for decay in (0.2, 0.5):
                result.append(
                    PlannerCandidate(
                        name=(
                            f"preview_soft_ap{acceleration_penalty:g}_"
                            f"jp{jerk_penalty:g}_d{decay:g}"
                        ),
                        strategy="preview_qp",
                        max_jerk_rad_s3=80.0,
                        acceleration_penalty=acceleration_penalty,
                        jerk_penalty=jerk_penalty,
                        preview_decay=decay,
                        enforce_derivative_limits=False,
                    )
                )
    return result


def _future_reference(
    chunk: HandTraceChunk,
    index: int,
    horizon_steps: int,
) -> np.ndarray:
    visible = chunk.raw_targets[index : index + horizon_steps]
    if len(visible) < horizon_steps:
        visible = np.vstack(
            [
                visible,
                np.repeat(
                    chunk.hold_target[None, :],
                    horizon_steps - len(visible),
                    axis=0,
                ),
            ]
        )
    return visible


def replay_shared(
    chunks: list[HandTraceChunk],
    candidate: PlannerCandidate,
    *,
    nominal_hz: float = 10.0,
) -> PlannerReplay:
    planner = CausalHandTrajectoryPlanner(candidate.config())
    commands_by_chunk: list[np.ndarray] = []
    anchors: list[np.ndarray | None] = []
    wait_commands: list[list[np.ndarray]] = [[] for _ in chunks]
    previous_request: int | None = None
    previous_chunk_index: int | None = None
    last_step_at: float | None = None
    held_target: np.ndarray | None = None
    simulated_command: np.ndarray | None = None
    converged = 0
    nonconverged = 0
    resets = 0
    nominal_dt = 1.0 / nominal_hz

    for chunk_index, chunk in enumerate(chunks):
        continuous = (
            previous_request is not None
            and chunk.request_id == previous_request + 1
        )
        if previous_request is None or not continuous:
            planner.reset(chunk.previous_command)
            simulated_command = (
                None
                if chunk.previous_command is None
                else chunk.previous_command.copy()
            )
            last_step_at = None
            held_target = None
            if previous_request is not None:
                resets += 1
        first_step_at = (
            chunk.segment_started_at + float(chunk.command_times[0])
            if len(chunk.command_times)
            else chunk.segment_started_at
        )
        if continuous and held_target is not None and last_step_at is not None:
            reconstructed_at = last_step_at + nominal_dt
            while reconstructed_at < first_step_at - 0.5 * nominal_dt:
                reference = np.repeat(
                    held_target[None, :], candidate.horizon_steps, axis=0
                )
                decision = planner.step(reference)
                simulated_command = decision.command.copy()
                if previous_chunk_index is not None:
                    wait_commands[previous_chunk_index].append(
                        simulated_command.copy()
                    )
                converged += int(decision.converged)
                nonconverged += int(not decision.converged)
                last_step_at = reconstructed_at
                reconstructed_at += nominal_dt

        anchors.append(
            None if simulated_command is None else simulated_command.copy()
        )
        commands = np.empty_like(chunk.raw_targets)
        for index, elapsed in enumerate(chunk.command_times):
            reference = _future_reference(
                chunk, index, candidate.horizon_steps
            )
            decision = planner.step(reference)
            commands[index] = decision.command
            simulated_command = decision.command.copy()
            converged += int(decision.converged)
            nonconverged += int(not decision.converged)
            last_step_at = chunk.segment_started_at + float(elapsed)
        commands_by_chunk.append(commands)
        held_target = chunk.hold_target.copy()
        previous_request = chunk.request_id
        previous_chunk_index = chunk_index

    return PlannerReplay(
        commands=commands_by_chunk,
        anchors=anchors,
        wait_commands=wait_commands,
        converged_steps=converged,
        nonconverged_steps=nonconverged,
        discontinuity_resets=resets,
    )


def _tail_and_wait_metrics(
    chunks: list[HandTraceChunk], replay: PlannerReplay
) -> dict[str, float | int]:
    tail_step_max_last3 = 0
    tail_step_chunks = 0
    high_steps = 0
    high_steps_last3 = 0
    tail_jerk_max_last3 = 0
    jerk_chunks = 0
    wait_cumulative: list[float] = []
    wait_steps: list[float] = []
    for chunk, commands, anchor, waits in zip(
        chunks,
        replay.commands,
        replay.anchors,
        replay.wait_commands,
        strict=True,
    ):
        sequence = commands if anchor is None else np.vstack([anchor, commands])
        if len(sequence) > 1:
            step = np.max(np.abs(np.diff(sequence, axis=0)), axis=1)
            location = int(np.argmax(step))
            tail_step_chunks += 1
            tail_step_max_last3 += int(location >= len(step) - 3)
            high = np.flatnonzero(step >= 0.12)
            high_steps += len(high)
            high_steps_last3 += int(np.count_nonzero(high >= len(step) - 3))
        if len(commands) >= 4:
            jerk = np.diff(commands, n=3, axis=0) * 10.0**3
            location, _ = np.unravel_index(
                int(np.argmax(np.abs(jerk))), jerk.shape
            )
            destination = int(location + 3)
            jerk_chunks += 1
            tail_jerk_max_last3 += int(destination >= len(commands) - 3)
        if waits and len(commands):
            wait_array = np.stack(waits)
            wait_sequence = np.vstack([commands[-1], wait_array])
            per_step = np.max(
                np.abs(np.diff(wait_sequence, axis=0)), axis=1
            )
            wait_steps.extend(per_step.tolist())
            wait_cumulative.append(
                float(np.max(np.abs(wait_array[-1] - commands[-1])))
            )

    def percentile(values: list[float], q: float) -> float:
        return 0.0 if not values else float(np.percentile(values, q))

    return {
        "chunk_max_step_last3_count": tail_step_max_last3,
        "chunk_max_step_location_chunks": tail_step_chunks,
        "high_step_ge_012_count": high_steps,
        "high_step_ge_012_last3_count": high_steps_last3,
        "chunk_max_jerk_last3_count": tail_jerk_max_last3,
        "chunk_max_jerk_location_chunks": jerk_chunks,
        "wait_windows": len(wait_cumulative),
        "wait_step_p95_rad": percentile(wait_steps, 95),
        "wait_step_max_rad": max(wait_steps, default=0.0),
        "wait_cumulative_p95_rad": percentile(wait_cumulative, 95),
        "wait_cumulative_max_rad": max(wait_cumulative, default=0.0),
    }


def _legacy_wait_cumulative_metrics(
    chunks: list[HandTraceChunk],
    commands: list[np.ndarray],
    anchors: list[np.ndarray | None],
) -> dict[str, float]:
    values: list[float] = []
    for index in range(len(chunks) - 1):
        if chunks[index + 1].request_id != chunks[index].request_id + 1:
            continue
        next_anchor = anchors[index + 1]
        if next_anchor is None or not len(commands[index]):
            continue
        values.append(
            float(
                np.max(np.abs(next_anchor - commands[index][-1]))
            )
        )
    return {
        "wait_cumulative_p95_rad": (
            0.0 if not values else float(np.percentile(values, 95))
        ),
        "wait_cumulative_max_rad": max(values, default=0.0),
    }


def evaluate(paths: list[Path]) -> dict[str, object]:
    runs = [(path, load_trace(path)) for path in paths]
    candidates = default_candidates()

    def conservative_aggregate(
        items: list[dict[str, float | int]],
    ) -> dict[str, float | int]:
        result: dict[str, float | int] = {}
        additive = {
            "commands",
            "missed_halfway_events",
            "halfway_events",
            "chunk_max_step_last3_count",
            "chunk_max_step_location_chunks",
            "high_step_ge_012_count",
            "high_step_ge_012_last3_count",
            "chunk_max_jerk_last3_count",
            "chunk_max_jerk_location_chunks",
            "wait_windows",
        }
        for key in items[0]:
            values = [item[key] for item in items]
            result[key] = (
                sum(int(value) for value in values)
                if key in additive
                else max(float(value) for value in values)
            )
        return result

    recorded = conservative_aggregate(
        [
            metrics(chunks, [chunk.recorded_commands for chunk in chunks])
            for _, chunks in runs
        ]
    )
    legacy_items: list[dict[str, float | int]] = []
    for _, chunks in runs:
        commands, anchors, _ = replay_legacy(
            chunks,
            LegacyCandidate("v1_1", 0.175, 1.75, 7.5, True),
        )
        item = metrics(chunks, commands, anchors=anchors)
        item.update(
            _tail_and_wait_metrics(
                chunks,
                PlannerReplay(
                    commands=commands,
                    anchors=anchors,
                    wait_commands=[[] for _ in chunks],
                ),
            )
        )
        item.update(
            _legacy_wait_cumulative_metrics(chunks, commands, anchors)
        )
        legacy_items.append(item)
    legacy = conservative_aggregate(legacy_items)
    payload: list[dict[str, object]] = []
    for candidate in candidates:
        run_metrics: list[dict[str, float | int]] = []
        solver = {
            "converged_steps": 0,
            "nonconverged_steps": 0,
            "discontinuity_resets": 0,
        }
        for _, chunks in runs:
            replay = replay_shared(chunks, candidate)
            item = metrics(
                chunks, replay.commands, anchors=replay.anchors
            )
            item.update(_tail_and_wait_metrics(chunks, replay))
            run_metrics.append(item)
            solver["converged_steps"] += replay.converged_steps
            solver["nonconverged_steps"] += replay.nonconverged_steps
            solver["discontinuity_resets"] += replay.discontinuity_resets
        payload.append(
            {
                "candidate": asdict(candidate),
                "metrics": conservative_aggregate(run_metrics),
                "solver": solver,
            }
        )
    eligible = [
        item
        for item in payload
        if item["metrics"]["missed_halfway_events"] == 0
        and not item["candidate"]["enforce_derivative_limits"]
        and item["solver"]["nonconverged_steps"] == 0
        and item["metrics"]["step_max_rad"]
        <= float(recorded["step_max_rad"]) + 1e-9
        and item["metrics"]["tracking_error_p95_rad"]
        <= float(legacy["tracking_error_p95_rad"])
        and item["metrics"]["halfway_shift_abs_p95_seconds"]
        <= float(legacy["halfway_shift_abs_p95_seconds"]) + 1e-9
        and abs(float(item["metrics"]["halfway_shift_max_early_seconds"]))
        <= abs(float(legacy["halfway_shift_max_early_seconds"])) + 1e-9
    ]
    selected = (
        None
        if not eligible
        else min(
            eligible,
            key=lambda item: (
                item["metrics"]["jerk_p95_rad_s3"],
                item["metrics"]["wait_cumulative_max_rad"],
                item["metrics"]["tracking_error_p95_rad"],
            ),
        )
    )
    recommendation = None
    if selected is not None:
        selected_metrics = selected["metrics"]
        recommendation = {
            "candidate": selected["candidate"]["name"],
            "gate": (
                "zero missed halfway events; zero solver nonconvergence; "
                "warning-only derivative limits; physical position constraints; "
                "step no worse than recorded and tracking/halfway no worse than "
                "V1.1 conservative run-max"
            ),
            "v1_1_jerk_p95_rad_s3": legacy["jerk_p95_rad_s3"],
            "candidate_jerk_p95_rad_s3": selected_metrics[
                "jerk_p95_rad_s3"
            ],
            "jerk_p95_reduction_fraction": 1.0
            - float(selected_metrics["jerk_p95_rad_s3"])
            / float(legacy["jerk_p95_rad_s3"]),
            "v1_1_tail_jerk_fraction": float(
                legacy["chunk_max_jerk_last3_count"]
            )
            / float(legacy["chunk_max_jerk_location_chunks"]),
            "candidate_tail_jerk_fraction": float(
                selected_metrics["chunk_max_jerk_last3_count"]
            )
            / float(selected_metrics["chunk_max_jerk_location_chunks"]),
            "v1_1_wait_cumulative_max_rad": legacy[
                "wait_cumulative_max_rad"
            ],
            "candidate_wait_cumulative_max_rad": selected_metrics[
                "wait_cumulative_max_rad"
            ],
        }
    return {
        "schema": "g2_shared_hand_planner_ablation_v1",
        "scope": "offline exact 10 Hz active targets plus reconstructed Sync wait; no GDK",
        "runs": [
            {
                "path": str(path),
                "chunks": len(chunks),
                "commands": sum(len(chunk.raw_targets) for chunk in chunks),
            }
            for path, chunks in runs
        ],
        "recorded_active": recorded,
        "v1_1_counterfactual": legacy,
        "candidates": payload,
        "recommendation": recommendation,
    }


def _print(report: dict[str, object]) -> None:
    print(
        "candidate                    stepMax jerkP95 trackP95 finalP95 "
        "halfMiss tailJerk waitMax nonconv"
    )
    for item in report["candidates"]:
        candidate = item["candidate"]
        metric = item["metrics"]
        solver = item["solver"]
        print(
            f"{candidate['name']:<28} "
            f"{metric['step_max_rad']:<7.4f} "
            f"{metric['jerk_p95_rad_s3']:<7.1f} "
            f"{metric['tracking_error_p95_rad']:<8.4f} "
            f"{metric['final_error_p95_rad']:<8.4f} "
            f"{metric['missed_halfway_events']}/{metric['halfway_events']:<7} "
            f"{metric['chunk_max_jerk_last3_count']}/{metric['chunk_max_jerk_location_chunks']:<7} "
            f"{metric['wait_cumulative_max_rad']:<7.4f} "
            f"{solver['nonconverged_steps']}"
        )
    print("recommendation", report["recommendation"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args.logs)
    _print(report)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
