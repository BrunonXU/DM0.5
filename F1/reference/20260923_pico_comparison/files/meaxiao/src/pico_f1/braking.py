"""Read-only configuration budget, NOT a physical braking certification."""
import math


def yaw_braking_budget(chassis, deceleration):
    spec = chassis['braking_check']
    values = {name: spec[name] for name in (
        'minimum_deceleration_rad_s2', 'response_allowance_s', 'max_overrun_deg')}
    values['actual_deceleration'] = deceleration
    values['body_speed'] = chassis['max_yaw_speed_rad_s']
    values['joystick_speed'] = chassis['joystick_max_yaw_speed_rad_s']
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'braking {name} must be positive and finite')
    if chassis.get('neutral_stop_mode') != 'zero_target' or chassis.get('body_follow_mode') != 'velocity':
        raise ValueError('braking budget requires zero_target and velocity body following')
    if deceleration < values['minimum_deceleration_rad_s2'] - 1e-9:
        raise ValueError('supplier angular deceleration below required minimum; do not auto-write parameters')
    omega = max(values['body_speed'], values['joystick_speed'])
    # Use the conservative configured floor even if the supplier reports more.
    a = values['minimum_deceleration_rad_s2']
    brake_angle = omega * omega / (2 * a)
    total = math.degrees(brake_angle + omega * values['response_allowance_s'])
    if total > values['max_overrun_deg'] + 1e-9:
        raise ValueError(f'estimated yaw stop budget {total:.3f} deg exceeds configured target')
    return dict(estimated_braking_deg=math.degrees(brake_angle),
                estimated_with_allowance_deg=total,
                configured_target_deg=values['max_overrun_deg'],
                response_allowance_s=values['response_allowance_s'],
                physical_validation='pending; parameter readback is not measured braking')
