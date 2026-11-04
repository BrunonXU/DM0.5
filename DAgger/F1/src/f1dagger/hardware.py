"""Explicit missing-left-gripper profile; model masking is a separate contract."""

LEFT_GRIPPER_REMOVED = {
    'schema': 'f1_hardware_capabilities_v1',
    'left_gripper_present': False,
    'right_gripper_present': True,
    'left_wrist_camera_present': True,
}


def state_mask(config):
    hardware = config.get('hardware_capabilities')
    if hardware is None:
        return (True,) * 16
    if hardware != LEFT_GRIPPER_REMOVED or any(type(hardware.get(k)) is not bool
            for k in ('left_gripper_present', 'right_gripper_present', 'left_wrist_camera_present')):
        raise ValueError('Unsupported hardware profile; only explicitly removed left gripper is supported')
    return (True,) * 14 + (False, True)


def validate_mask(mask):
    if mask is None:
        return (True,) * 16
    if len(mask) != 16 or any(type(v) is not bool for v in mask) or tuple(mask) != (True,) * 14 + (False, True):
        raise ValueError('Invalid missing-hardware field mask')
    return tuple(mask)
