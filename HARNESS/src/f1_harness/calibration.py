"""Chessboard and robot geometry solvers with independent holdout checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from .contracts import digest
from .geometry import transform


def board_points(inner=(9, 6), square_m=.022):
    if min(inner) < 3 or square_m <= 0:
        raise ValueError("invalid board")
    points = np.zeros((inner[0] * inner[1], 3), np.float32)
    points[:, :2] = np.mgrid[0:inner[0], 0:inner[1]].T.reshape(-1, 2) * square_m
    return points


def detect_board(image, inner=(9, 6)):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    found, corners = cv2.findChessboardCornersSB(gray, inner, flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY)
    if not found:
        raise ValueError("complete checkerboard not detected")
    return corners.astype(np.float32)


def _image_points(paths, inner):
    corners, size, hashes = [], None, []
    for path in paths:
        data = Path(path).read_bytes()
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("unreadable calibration image")
        current_size = image.shape[1], image.shape[0]
        if size and size != current_size:
            raise ValueError("mixed calibration resolutions")
        size = current_size
        corners.append(detect_board(image, inner))
        hashes.append(hashlib.sha256(data).hexdigest())
    return corners, size, hashes


def solve_intrinsics(train, holdout, *, inner=(9, 6), square_m=.022, max_error_px=.5):
    if len(train) < 8 or len(holdout) < 3:
        raise ValueError("need >=8 training views and >=3 independent holdout views")
    train_corners, size, train_hashes = _image_points(train, inner)
    test_corners, test_size, test_hashes = _image_points(holdout, inner)
    if size != test_size or set(train_hashes) & set(test_hashes):
        raise ValueError("holdout images must be independent and use the same resolution")
    points = board_points(inner, square_m)
    rms, k, distortion, _, _ = cv2.calibrateCamera([points] * len(train), train_corners, size, None, None)
    errors = []
    for corners in test_corners:
        ok, rvec, tvec = cv2.solvePnP(points, corners, k, distortion)
        if not ok:
            raise ValueError("holdout PnP failed")
        projected, _ = cv2.projectPoints(points, rvec, tvec, k, distortion)
        errors.append(float(np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=-1)))))
    report = dict(kind="intrinsics", resolution=size, inner_corners=inner, square_m=square_m,
                  k=k.tolist(), distortion=distortion.ravel().tolist(), train_rms_px=float(rms),
                  holdout_rms_px=errors, max_error_px=max_error_px, train_hashes=train_hashes,
                  holdout_hashes=test_hashes, accepted=max([rms, *errors]) <= max_error_px,
                  proves_metric_tcp_accuracy=False)
    report["calibration_id"] = digest(report)
    return report


def board_pose(image, calibration, *, inner=(9, 6), square_m=.022):
    corners = detect_board(image, inner)
    k, distortion = np.array(calibration["k"]), np.array(calibration["distortion"])
    ok, rvec, tvec = cv2.solvePnP(board_points(inner, square_m), corners, k, distortion)
    if not ok:
        raise ValueError("PnP failed")
    t = np.eye(4)
    t[:3, :3], t[:3, 3] = cv2.Rodrigues(rvec)[0], tvec.ravel()
    return t


def solve_handeye(train, holdout, *, max_translation_m, max_rotation_deg):
    """Eye-in-hand: input T_base_eef and T_camera_board, output T_eef_camera."""
    if len(train) < 8 or len(holdout) < 3:
        raise ValueError("need >=8 robot poses plus >=3 independent holdout poses")
    if set(map(digest, train)) & set(map(digest, holdout)):
        raise ValueError("train/holdout overlap")
    eef = [transform(row["base_eef"]) for row in train]
    camera = [transform(row["camera_board"]) for row in train]
    rotations = np.array([Rotation.from_matrix(t[:3, :3]).as_rotvec() for t in eef])
    if np.linalg.matrix_rank(rotations - rotations.mean(axis=0), tol=.02) < 2:
        raise ValueError("hand-eye poses need rotations about multiple axes")
    r, t = cv2.calibrateHandEye([v[:3, :3] for v in eef], [v[:3, 3] for v in eef],
                               [v[:3, :3] for v in camera], [v[:3, 3] for v in camera],
                               method=cv2.CALIB_HAND_EYE_PARK)
    eef_camera = np.eye(4)
    eef_camera[:3, :3], eef_camera[:3, 3] = r, t.ravel()
    transform(eef_camera)
    boards = [a @ eef_camera @ b for a, b in zip(eef, camera)]
    mean = np.eye(4)
    mean[:3, :3] = Rotation.from_matrix(np.array([v[:3, :3] for v in boards])).mean().as_matrix()
    mean[:3, 3] = np.mean([v[:3, 3] for v in boards], axis=0)
    errors = []
    for row in holdout:
        predicted = transform(row["base_eef"]) @ eef_camera @ transform(row["camera_board"])
        residual = np.linalg.inv(mean) @ predicted
        errors.append(dict(translation_m=float(np.linalg.norm(residual[:3, 3])),
                           rotation_deg=float(np.degrees(Rotation.from_matrix(residual[:3, :3]).magnitude()))))
    report = dict(kind="handeye", eef_camera=eef_camera.tolist(), base_board=mean.tolist(),
                  holdout_errors=errors, accepted=all(e["translation_m"] <= max_translation_m and
                  e["rotation_deg"] <= max_rotation_deg for e in errors),
                  max_translation_m=max_translation_m, max_rotation_deg=max_rotation_deg,
                  train_hash=digest(train), holdout_hash=digest(holdout))
    report["calibration_id"] = digest(report)
    return report


def solve_tcp(train, holdout, *, max_error_m):
    """Pivot calibration: solve R*p_tcp + t = fixed physical pivot point."""
    if len(train) < 6 or len(holdout) < 3 or set(map(digest, train)) & set(map(digest, holdout)):
        raise ValueError("need >=6 distinct training and >=3 independent holdout poses")
    transforms = [transform(row) for row in train]
    a = np.vstack([np.column_stack((t[:3, :3], -np.eye(3))) for t in transforms])
    b = np.concatenate([-t[:3, 3] for t in transforms])
    solution, _, rank, _ = np.linalg.lstsq(a, b, rcond=None)
    if rank < 6:
        raise ValueError("degenerate TCP calibration poses")
    errors = [float(np.linalg.norm(transform(row)[:3, :3] @ solution[:3] +
                                   transform(row)[:3, 3] - solution[3:])) for row in holdout]
    report = dict(kind="tcp", eef_tcp_translation_m=solution[:3].tolist(), pivot_base_m=solution[3:].tolist(),
                  holdout_errors_m=errors, accepted=max(errors) <= max_error_m, max_error_m=max_error_m,
                  train_hash=digest(train), holdout_hash=digest(holdout))
    report["calibration_id"] = digest(report)
    return report


def validate_bundle(bundle):
    """Motion admission additionally needs real robot checks; image RMS alone is insufficient."""
    required = ("head_intrinsics", "right_intrinsics", "right_handeye", "tcp", "head_extrinsics",
                "rgbd_registration", "native_frame_chain", "scene_geometry", "tracking_validation")
    reasons = []
    for name in required:
        report = bundle.get(name)
        if not isinstance(report, dict) or not report.get("accepted") or report.get("synthetic", False):
            reasons.append(name + ":unvalidated")
        elif not report.get("evidence_ids") and not report.get("holdout_hashes") and not report.get("holdout_hash"):
            reasons.append(name + ":missing_independent_evidence")
    clearance, error = bundle.get("effective_clearance_m"), bundle.get("combined_error_bound_m")
    if not isinstance(clearance, (int, float)) or not isinstance(error, (int, float)) or not 0 < error < clearance:
        reasons.append("insertion_error_budget_not_met")
    return dict(accepted=not reasons, reasons=reasons, calibration_id=digest(bundle))


def save_report(path: Path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")


def solve_rigid_alignment(train, holdout, *, max_error_m):
    """Head/external frame registration from measured 3D correspondences and held-out points."""
    if len(train)<4 or len(holdout)<3 or set(map(digest,train))&set(map(digest,holdout)):
        raise ValueError('independent 3D correspondence sets required')
    source=np.asarray([r['source_m'] for r in train],float)
    target=np.asarray([r['target_m'] for r in train],float)
    if source.shape!=target.shape or source.shape[1:]!=(3,) or not np.isfinite([source,target]).all():
        raise ValueError('invalid metric points')
    a,b=source-source.mean(0),target-target.mean(0)
    if np.linalg.matrix_rank(a,tol=1e-5)<2:raise ValueError('collinear calibration points')
    u,_,vt=np.linalg.svd(a.T@b)
    correction=np.eye(3);correction[-1,-1]=np.linalg.det(vt.T@u.T)
    rotation=vt.T@correction@u.T
    t=np.eye(4);t[:3,:3]=rotation;t[:3,3]=target.mean(0)-rotation@source.mean(0)
    errors=[float(np.linalg.norm(rotation@np.asarray(r['source_m'])+t[:3,3]-r['target_m'])) for r in holdout]
    report=dict(kind='rigid_alignment',target_source=t.tolist(),holdout_errors_m=errors,
                max_error_m=max_error_m,accepted=max(errors)<=max_error_m,
                train_hash=digest(train),holdout_hash=digest(holdout))
    report['calibration_id']=digest(report)
    return report
