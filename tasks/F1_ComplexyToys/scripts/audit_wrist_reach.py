#!/usr/bin/env python3
"""Necessary wrist-center reach bound derived from the frozen modified DH model."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from f1_toys.eef import relative_pose
from f1_toys.native.fast_kinematics import FastArmModel


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    config = Path(__file__).resolve().parents[1]/"src/f1_toys/native/ccs_m6_40.MvKDCfg"
    model = FastArmModel(config)
    # Joint 1--3 axes meet at S; joint 5--7 meet at W. The two rigid
    # displacements S->joint4 and joint4->W have these lengths.
    dh = model.dh[1]
    np.testing.assert_allclose(dh[:, 1:3], [
        [0, .1745], [0, 0], [0, .287], [.018, 0],
        [.018, .314], [0, 0], [0, 0], [0, .095]], atol=1e-14)
    shoulder = np.array([0., 0., dh[0, 2]])
    upper = np.hypot(dh[2, 2], dh[3, 1])
    forearm = np.hypot(dh[4, 2], dh[4, 1])
    radius = upper+forearm
    flange = dh[7, 2]
    def reach(pose):
        wrist = pose[:3, 3]-flange*pose[:3, 2]
        return np.linalg.norm(wrist-shoulder)
    # Independently check the necessary bound across random forward kinematics.
    rng = np.random.default_rng(20261020)
    for _ in range(1000):
        q = rng.uniform(model.low, model.high)
        assert reach(model.fk_rad(1, q)) <= radius+1e-12
    straight_elbow = -(np.arctan2(dh[3, 1], dh[2, 2])
                       + np.arctan2(dh[4, 1], dh[4, 2]))
    q = np.deg2rad([-13, -77, 64, 0, 19, 2, -4])
    q[3] = straight_elbow
    assert model.admissible_rad(q)
    np.testing.assert_allclose(reach(model.fk_rad(1, q)), radius, atol=1e-12, rtol=0)
    # Even with original pose tolerances, W can move by at most this amount.
    allowable_wrist_error_mm = .01+2*flange*1000*np.sin(np.deg2rad(.001)/2)
    results = []
    for row in json.loads(a.inputs.read_text()):
        pose = np.asarray(row["target"])
        distance = reach(pose)
        excess = (distance-radius)*1000
        results.append(dict(
            episode=row["episode"], request_id=row["request_id"],
            target_wrist_distance_mm=distance*1000, radial_excess_mm=excess,
            violates_bound_even_with_original_pose_tolerances=bool(excess > allowable_wrist_error_mm),
            original_position_error_mm=row["saved_candidate"]["position_error_mm"]))
    manifest = dict(
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        inputs_sha256=hashlib.sha256(a.inputs.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        shoulder_m=shoulder.tolist(), upper_length_mm=upper*1000,
        forearm_length_mm=forearm*1000, radius_mm=radius*1000,
        flange_offset_along_output_z_mm=flange*1000,
        straight_elbow_deg=float(np.rad2deg(straight_elbow)),
        allowed_wrist_error_mm=allowable_wrist_error_mm,
        random_fk_checks=1000, within_limits_radius_witness_verified=True,
        impossible_targets=sum(r["violates_bound_even_with_original_pose_tolerances"] for r in results),
        robot_commands=0, deployment_changes=0,
        proof="Triangle inequality: ||W-S|| <= hypot(287,18)+hypot(314,18); W=p-95*R[:,2] in mm.",
        limitation="A necessary geometric bound for the frozen model, not a physical calibration or collision certificate.")
    (a.output/"results.json").write_text(json.dumps(results, indent=2)+"\n")
    (a.output/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps(manifest), flush=True)
    print(json.dumps([r for r in results if r["violates_bound_even_with_original_pose_tolerances"]]), flush=True)


if __name__ == "__main__":
    main()
