"""Read already measured grasp evidence during the normal lift; no polling IO."""


def lift_evidence(observation, *, object_id, calibration_id, after_ns, gripper, gripper_tolerance):
    if (observation is None or observation.calibration_id!=calibration_id or observation.faults
            or observation.state_received_ns is None or observation.state_received_ns<=after_ns
            or not observation.state_valid_mask[15]
            or abs(observation.state16[15]-gripper)>gripper_tolerance
            or not observation.frames or min(f.received_ns for f in observation.frames)<=after_ns):
        return None
    target=next((o for o in observation.objects if o.object_id==object_id),None)
    if target is None or not target.visible or not target.evidence_ids:
        return None
    return target.held
