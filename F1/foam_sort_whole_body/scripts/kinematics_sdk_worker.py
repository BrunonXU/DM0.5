#!/usr/bin/env python3
"""Isolated F1 libKine mathematics worker: no ROS publishers or motor SDK.

Run on the SDK's native architecture. Importing does not load or initialize it.
The ABI is pinned to reference/20260916_kinematics/ FXKineCommon.h (992 bytes).
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import time

import numpy as np

D = C.c_double
V7 = D * 7
M4 = (D * 4) * 4


def apply_controller_limits(pnva):
    """LOADMvCfg returns PNVA columns positive, negative, velocity, acceleration."""
    out = np.asarray(pnva, dtype=np.float64).copy()
    if out.shape != (2, 7, 4):
        raise ValueError("Expected loaded PNVA [2,7,4]")
    out[:, :, 0] = [178, 120, 178, 60, 178, 60, 90]
    out[:, :, 1] = [-178, -94, -178, -145, -178, -60, -90]
    return out


class SolveParams(C.Structure):
    _fields_ = [
        ("target", M4), ("reference", V7), ("nsp_type", C.c_int32),
        ("nsp_params", D * 6), ("nsp_angle", D),
        ("dgr1", D), ("dgr2", D), ("dgr3", D),
        ("joints", V7), ("all_joints", (D * 8) * 8),
        ("result_num", C.c_int32), ("out_range", C.c_ubyte),
        ("singular", C.c_ubyte * 7), ("exceeded_tags", C.c_ubyte * 7),
        ("exceeded_abs", D), ("exceeded", C.c_ubyte),
        ("limit_pos", V7), ("limit_neg", V7),
    ]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NativeKinematics:
    """Independent process-local SDK context, matching service zero-NSP-angle path."""

    def __init__(self, library, config, *, controller_limits=False):
        if C.sizeof(SolveParams) != 992:
            raise RuntimeError("Unexpected IK ABI layout")
        self.lib = C.CDLL(str(library))
        signatures = {
            "LOADMvCfg": [C.c_char_p] + [C.c_void_p] * 8,
            "FX_Robot_Init_Type": [C.c_int32, C.c_int32],
            "FX_Robot_Init_Kine": [C.c_int32, C.c_void_p],
            "FX_Robot_Init_Lmt": [C.c_int32, C.c_void_p, C.c_void_p],
            "FX_Robot_Kine_FK": [C.c_int32, C.c_void_p, C.c_void_p],
            "FX_Robot_Kine_IK": [C.c_int32, C.POINTER(SolveParams)],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, C.c_ubyte
        self.lib.FX_LOG_SWITCH.argtypes = [C.c_int32]
        self.lib.FX_LOG_SWITCH.restype = None
        self.lib.FX_LOG_SWITCH(0)
        arrays = [np.zeros(2, np.int32)] + [np.zeros(shape, np.float64) for shape in
                  [(2, 3), (2, 8, 4), (2, 7, 4), (2, 4, 3), (2, 7), (2, 7, 3), (2, 7, 6)]]
        if self.lib.LOADMvCfg(str(config).encode(), *[a.ctypes.data for a in arrays]) != 1:
            raise RuntimeError("LOADMvCfg failed")
        robot_type, _, dh, pnva, bounds, *_ = arrays
        source_pnva = pnva.copy()
        if controller_limits:
            # Diagnostic process-local override only: never edit the robot config.
            pnva = apply_controller_limits(pnva)
        if not np.all(pnva[:, :, 0] > pnva[:, :, 1]):
            raise ValueError("SDK PNVA positive/negative limit order is invalid")
        for arm in (0, 1):
            for ok in [self.lib.FX_Robot_Init_Type(arm, int(robot_type[arm])),
                       self.lib.FX_Robot_Init_Kine(arm, dh[arm].ctypes.data),
                       self.lib.FX_Robot_Init_Lmt(arm, pnva[arm].ctypes.data, bounds[arm].ctypes.data)]:
                if ok != 1:
                    raise RuntimeError(f"SDK initialization failed for arm {arm}")
        self.contract = {
            "library_path": str(library), "library_sha256": sha256(library),
            "config_path": str(config), "config_sha256": sha256(config),
            "ik_struct_bytes": C.sizeof(SolveParams), "joint_unit": "degree",
            "sdk_translation_unit": "mm", "cache_translation_unit": "m",
            "frame": "native_per_arm_FK_reference", "tool": "SDK default; service parity required",
            "nsp_type": 0, "nsp_angle_deg": 0.0,
            "config_pnva": source_pnva.tolist(), "effective_pnva": pnva.tolist(),
            "controller_limits_override": controller_limits, "config_j67_bounds": bounds.tolist(),
        }

    def fk(self, arm, joints):
        q = np.ascontiguousarray(joints, dtype=np.float64)
        if arm not in (0, 1) or q.shape != (7,) or not np.isfinite(q).all():
            raise ValueError("FK requires one finite 7D arm in degrees")
        pose = np.full((4, 4), np.nan, np.float64)
        ok = self.lib.FX_Robot_Kine_FK(arm, q.ctypes.data, pose.ctypes.data)
        if ok != 1 or not np.isfinite(pose).all():
            raise RuntimeError("Native FK failed")
        pose[:3, 3] /= 1000.0
        return pose

    def ik(self, arm, pose_m, reference, *, include_candidates=False):
        pose = np.asarray(pose_m, np.float64).copy()
        ref = np.asarray(reference, np.float64)
        if arm not in (0, 1) or pose.shape != (4, 4) or ref.shape != (7,) or not np.isfinite(pose).all() or not np.isfinite(ref).all():
            raise ValueError("Invalid IK input")
        pose[:3, 3] *= 1000.0
        params = SolveParams()  # service also zero-initializes the complete structure
        C.memmove(C.addressof(params.target), pose.ctypes.data, pose.nbytes)
        params.reference[:] = ref
        params.nsp_type = 0
        start = time.perf_counter_ns()
        ok = self.lib.FX_Robot_Kine_IK(arm, C.byref(params)) == 1
        latency_us = (time.perf_counter_ns() - start) / 1000.0
        result = {
            "success": ok, "joints": list(params.joints),
            "result_num": int(params.result_num), "out_range": bool(params.out_range),
            "singular": [bool(x) for x in params.singular],
            "joint_exceeded": bool(params.exceeded),
            "joint_exceeded_tags": [bool(x) for x in params.exceeded_tags],
            "limit_pos": list(params.limit_pos), "limit_neg": list(params.limit_neg),
            "latency_us": latency_us,
        }
        if include_candidates:
            result["all_joints_raw"] = np.ctypeslib.as_array(params.all_joints).tolist()
        return result


def pose_error(target, actual):
    position_mm = float(np.linalg.norm(target[:3, 3] - actual[:3, 3]) * 1000)
    rotation = target[:3, :3].T @ actual[:3, :3]
    angle_deg = float(np.degrees(np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1))))
    return position_mm, angle_deg


def relative_roundtrip(anchor, target):
    """Independently exercise the recipe's row-major rotation6D and fixed anchor."""
    delta = np.linalg.inv(anchor) @ target
    rot6 = delta[:2, :3].reshape(6)
    row1 = rot6[:3] / np.linalg.norm(rot6[:3])
    row2 = rot6[3:] - np.dot(row1, rot6[3:]) * row1
    row2 /= np.linalg.norm(row2)
    recovered = np.eye(4)
    recovered[:3, :3] = np.stack([row1, row2, np.cross(row1, row2)])
    recovered[:3, 3] = delta[:3, 3]
    return anchor @ recovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", default="/home/user/workspace/f1/install/tj_sdk/lib/libKine.so")
    parser.add_argument("--config", default="/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg")
    parser.add_argument("--requests", type=Path, required=True, help="JSONL of finite FK/IK numeric requests")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controller-limits", action="store_true", help="process-local diagnostic; installed config is unchanged")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    sdk = NativeKinematics(args.library, args.config, controller_limits=args.controller_limits)
    chains = {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.requests.open() as src, args.output.open("x") as dst:
        dst.write(json.dumps({"contract": sdk.contract, "requests_sha256": sha256(args.requests)}) + "\n")
        for line in src:
            req = json.loads(line)
            arm = req["arm"]
            target = sdk.fk(arm, req["joints"])
            result = {"id": req["id"], "arm": arm, "pose": target.tolist()}
            if "reference" in req:
                chain = req.get("chain")
                reference = chains.get(chain, req["reference"]) if chain else req["reference"]
                anchor = sdk.fk(arm, req.get("anchor_joints", req["reference"]))
                reconstructed = relative_roundtrip(anchor, target)
                result["encoding_error"] = pose_error(target, reconstructed)
                ik = sdk.ik(arm, reconstructed, reference, include_candidates=req.get("include_candidates", False))
                ik["reference"] = list(reference)
                if ik["success"] and np.isfinite(ik["joints"]).all():
                    ik["pose_error"] = pose_error(target, sdk.fk(arm, ik["joints"]))
                    if chain and not ik["out_range"] and not ik["joint_exceeded"]:
                        chains[chain] = ik["joints"]
                result["ik"] = ik
            dst.write(json.dumps(result, allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.output), "sha256": sha256(args.output)}))


if __name__ == "__main__":
    main()
