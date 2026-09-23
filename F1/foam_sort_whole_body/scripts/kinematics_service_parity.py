#!/usr/bin/env python3
"""Compare process-local libKine with existing read-only FK/IK ROS services."""
import argparse
import json
from pathlib import Path
import time

import numpy as np

from kinematics_sdk_worker import NativeKinematics, pose_error, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    import rclpy
    from f1_msgs.srv import ForwardKinematics, InverseKinematics

    sdk = NativeKinematics("/home/user/workspace/f1/install/tj_sdk/lib/libKine.so",
                           "/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg")
    rclpy.init()
    node = rclpy.create_node("f1_kinematics_readonly_parity")
    clients = {"fk": node.create_client(ForwardKinematics, "/kinematics/forward_kinematics"),
               "ik": node.create_client(InverseKinematics, "/kinematics/inverse_kinematics")}
    report = {"contract": sdk.contract, "requests_sha256": sha256(args.requests), "cases": [],
              "robot_commands_sent": 0}

    def call(kind, request):
        client = clients[kind]
        if not client.wait_for_service(timeout_sec=5.0):
            raise RuntimeError(f"{kind} service unavailable")
        before = time.perf_counter()
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=5.0)
        if not future.done():
            client.remove_pending_request(future)
            raise TimeoutError(kind)
        return future.result(), (time.perf_counter() - before) * 1000

    try:
        for line in args.requests.read_text().splitlines():
            req = json.loads(line)
            arm, q = req["arm"], req["joints"]
            local = sdk.fk(arm, q)
            request = ForwardKinematics.Request(target_arm=arm, joint_positions=q, compute_nsp=False)
            response, fk_ms = call("fk", request)
            if not response.success:
                raise RuntimeError(response.message)
            # ROS float arrays expose a writable buffer; unit conversion must not
            # mutate response.pose_matrix, which is subsequently sent in SDK mm.
            actual = np.asarray(response.pose_matrix).reshape(4, 4).copy()
            actual[:3, 3] /= 1000
            ik_req = InverseKinematics.Request(target_arm=arm, target_pose=list(response.pose_matrix),
                                              target_xyzabc=[], ref_joints=req.get("reference", q),
                                              use_nsp=True, nsp_type=0, nsp_params=[], nsp_angle=0.0)
            ik_resp, ik_ms = call("ik", ik_req)
            ik_local = sdk.ik(arm, actual, req.get("reference", q))
            report["cases"].append({"id": req["id"], "arm": arm, "fk_error": pose_error(local, actual),
                "fk_matrix_max_abs_diff": float(np.max(np.abs(local-actual))),
                "ros_success": bool(ik_resp.success), "sdk_success": ik_local["success"],
                "ik_joint_max_abs_diff_deg": float(np.max(np.abs(np.array(ik_resp.joint_positions)-ik_local["joints"]))) if len(ik_resp.joint_positions)==7 else None,
                "ros_joints": list(ik_resp.joint_positions), "sdk_joints": ik_local["joints"],
                "ros_limit_pos": list(ik_resp.joint_limit_pos), "ros_limit_neg": list(ik_resp.joint_limit_neg),
                "ros_joint_exceeded": bool(ik_resp.is_joint_exceeded), "sdk_joint_exceeded": ik_local["joint_exceeded"],
                "ros_out_range": bool(ik_resp.is_out_range), "sdk_out_range": ik_local["out_range"],
                "ros_singular": list(ik_resp.is_singular), "sdk_singular": ik_local["singular"],
                "fk_service_ms": fk_ms, "ik_service_ms": ik_ms})
    finally:
        node.destroy_node()
        rclpy.shutdown()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2)+"\n")
    cases = report["cases"]
    passed = bool(cases) and all(c["fk_matrix_max_abs_diff"] < 1e-9 and c["ros_success"] == c["sdk_success"]
        and c["ik_joint_max_abs_diff_deg"] is not None and c["ik_joint_max_abs_diff_deg"] < 1e-7
        and c["ros_joint_exceeded"] == c["sdk_joint_exceeded"]
        and c["ros_out_range"] == c["sdk_out_range"] and c["ros_singular"] == c["sdk_singular"] for c in cases)
    print(json.dumps({"cases":len(cases),"parity_passed":passed,"output":str(args.output)}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
