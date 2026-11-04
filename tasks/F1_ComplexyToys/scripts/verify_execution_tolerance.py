#!/usr/bin/env python3
"""Independent acceptance check of saved offline IK alternatives, no execution."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from f1_toys.eef import relative_pose
from f1_toys.native.kinematics import ArmModel, pose_error


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    assert not a.output.exists()
    task = Path(__file__).resolve().parents[1]
    model = ArmModel(task/"src/f1_toys/native/ccs_m6_40.MvKDCfg")
    rows = {(x["episode"], x["server"]["request_id"]): x["server"]
            for x in map(json.loads, a.source.read_text().splitlines())}
    counts = dict(manifests=0, accepted_points=0, unchanged_controls=0,
                  committed_prefixes=0, successful_windows=0)
    for directory in a.root.iterdir():
        if not directory.is_dir() or not (directory/"manifest.json").exists():
            continue
        manifest = json.loads((directory/"manifest.json").read_text())
        assert manifest["source_sha256"] == hashlib.sha256(a.source.read_bytes()).hexdigest()
        revision = directory.name.rsplit("-", 1)[1]
        snapshot = a.root/("audit_execution_tolerance-"+revision+".py")
        assert manifest["script_sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
        assert all(manifest[k] == 0 for k in ("robot_commands","model_calls","gpu_calls","deployment_changes"))
        counts["manifests"] += 1
        if not directory.name.startswith("projection"):
            continue
        for result_file in directory.glob("*.json"):
            if result_file.name in ("manifest.json", "summary.json", "progress.json"):
                continue
            result = json.loads(result_file.read_text())
            budget = result["summary"]["budget"]
            accepted_failures = 0
            for record in result["requests"]:
                source = rows[record["episode"], record["request_id"]]
                prefix = source["rtc_request"]["committed_actions16"]
                q = np.asarray(record["joints"])
                if prefix:
                    np.testing.assert_array_equal(q[:len(prefix)],np.asarray(prefix)[:,7:14])
                    counts["committed_prefixes"] += 1
                if record["success"]:
                    assert len(q) == source["rtc_request"]["coverage_steps"]
                    counts["successful_windows"] += 1
                    accepted_failures += not source["success"]
                if source["success"]:
                    assert record["success"]
                    np.testing.assert_allclose(q,np.asarray(source["actions"])[:,7:14],atol=1e-7,rtol=0)
                    assert not any(d["projected"] for d in record["diagnostics"])
                    counts["unchanged_controls"] += 1
                for d in record["diagnostics"]:
                    if not d["success"]:
                        continue
                    step = d["step_1_based"]-1
                    angle = np.asarray(d["joints"])
                    assert model.admissible_rad(np.deg2rad(angle))
                    assert np.all(angle >= np.array([-178,-94,-178,-145,-178,-60,-90])-1e-7)
                    assert np.all(angle <= np.array([178,120,178,60,178,60,90])+1e-7)
                    assert 1.025*abs(angle[5])+abs(angle[6]) <= 110.5+1e-7
                    target = relative_pose(np.asarray(source["absolute_eef10"][step]))
                    errors = pose_error(target,model.fk(1,angle))
                    limit = budget if d["projected"] else (.01,.001)
                    assert errors[0] <= limit[0]+1e-7 and errors[1] <= limit[1]+1e-7
                    np.testing.assert_allclose(errors,[d["position_error_mm"],d["rotation_error_deg"]],
                                               atol=1e-7,rtol=1e-6)
                    counts["accepted_points"] += 1
            assert accepted_failures == result["summary"]["full_windows_pass"]
    # Independent finite differences of the squared normalized residual Jacobian
    # used by the epigraph projection constraints.
    source = next(x for x in rows.values() if not x["success"])
    target = relative_pose(np.asarray(source["absolute_eef10"][25]))
    q = np.deg2rad(source["prediction_replay"]["state16"][7:14])
    scales = np.array([.001,np.deg2rad(.5)])
    def values(qi):
        e,_=model.residual(1,qi,target)
        return np.array([e[:3]@e[:3],e[3:]@e[3:]])/scales**2
    e,j=model.residual(1,q,target)
    analytic=np.vstack([2*e[:3]@j[:3],2*e[3:]@j[3:]])/scales[:,None]**2
    finite=np.column_stack([(values(q+np.eye(7)[k]*1e-6)-values(q-np.eye(7)[k]*1e-6))/2e-6 for k in range(7)])
    np.testing.assert_allclose(analytic,finite,atol=1e-3,rtol=1e-5)
    a.output.write_text(json.dumps(dict(passed=True,counts=counts,
        checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        checked_with="ArmModel reference FK, explicit original bounds, original source EEF/prefix",
        projection_jacobian_finite_difference=True,robot_commands=0,model_calls=0,
        limitations="Numerical artifact acceptance only; no physical collision/contact/dynamic certification."),indent=2)+"\n")
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
