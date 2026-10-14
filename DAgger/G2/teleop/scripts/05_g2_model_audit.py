#!/usr/bin/env python3
"""Audit the vendor G2/O10 model locally on G2 without importing GDK."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from g2teleop.model_audit import build_reduced_arm_model, missing_names, sha256_file


DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "configs" / "g2_kinematics_model.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only G2 Pinocchio model audit")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    try:
        import pinocchio as pin
    except ImportError as exc:
        raise RuntimeError("Pinocchio is required for this read-only audit") from exc

    sources = contract["sources"]
    hashes: dict[str, dict[str, object]] = {}
    for label, source in sources.items():
        path = Path(source["path_on_g2"])
        observed = sha256_file(path)
        hashes[label] = {
            "path": str(path), "expected": source["sha256"],
            "observed": observed, "matches": observed == source["sha256"],
        }
    if not all(item["matches"] for item in hashes.values()):
        raise RuntimeError("vendor model hash mismatch; refuse to continue")

    model = pin.buildModelFromMJCF(sources["full_o10_mjcf"]["path_on_g2"])
    frame_names = [frame.name for frame in model.frames]
    joint_contract = contract["controlled_arm_joints"]
    frame_contract = contract["candidate_frames"]
    expected_frames = [name for name in frame_contract.values() if name]
    missing_joints = missing_names(
        model.names, joint_contract["left"] + joint_contract["right"]
    )
    missing_frames = missing_names(frame_names, expected_frames)
    if missing_joints or missing_frames:
        raise RuntimeError(
            f"model contract mismatch: joints={missing_joints}, frames={missing_frames}"
        )

    left = build_reduced_arm_model(pin, model, joint_contract["left"])
    right = build_reduced_arm_model(pin, model, joint_contract["right"])
    report = {
        "read_only": True,
        "gdk_imported": False,
        "pinocchio_version": pin.__version__,
        "hashes": hashes,
        "full": {
            "nq": model.nq, "nv": model.nv,
            "njoints": model.njoints, "nframes": model.nframes,
        },
        "reduced": {
            "left": {"nq": left.nq, "nv": left.nv},
            "right": {"nq": right.nq, "nv": right.nv},
        },
        "candidate_frames_present": expected_frames,
        "result": "PASS_MODEL_STRUCTURE_ONLY_FK_NOT_YET_VALIDATED",
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
