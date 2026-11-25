"""Independent blocked closure is contact evidence, never command echo evidence."""
import numpy as np

from .geometry import transform


def fresh_position(packet, *, frame_ns, max_age_ns, boot_id):
    if hasattr(packet, 'model_dump'):
        packet=packet.model_dump()
    if (not isinstance(packet,dict) or packet.get('source')!='socketcan_external'
            or packet.get('protocol')!='omnipicker_status_v1' or packet.get('side')!='right'
            or not packet.get('running') or not boot_id or packet.get('boot_id')!=boot_id):
        return None
    rows=[r for r in packet.get('samples',()) if r.get('time_valid')
          and r.get('source_instance')==packet.get('source_instance')
          and 0<=frame_ns-r['received_ns']+r['queue_delay_ns']<=max_age_ns]
    return rows[-1]['closure_percent'] if rows else None


def measurement_summary(packet, frame_ns):
    """Bounded numeric model input; the full CAN window stays in local evidence."""
    if packet is None:
        return dict(available=False, source='socketcan_external', reason='no_independent_sample')
    value = packet.model_dump() if hasattr(packet, 'model_dump') else packet
    rows = [r for r in value['samples'] if r['time_valid']
            and r['received_ns']-r['queue_delay_ns'] <= frame_ns]
    last = rows[-1] if rows else None
    result = dict(available=bool(value['running'] and last), source=value['source'],
        unit=value['position_unit'], legacy_state16_15_source='mixed_command_and_feedback')
    if last:
        stamp=last['received_ns']-last['queue_delay_ns']
        result.update(closure=last['closure_percent'], received_ns=stamp,
            age_at_image_ns=frame_ns-stamp, sequence=last['sequence'],
            source_instance=value['source_instance'])
    if value.get('error'):
        result['reason']=value['error']
    return result


def blocked_closure(packet, *, frame_ns, after_ns, target, open_max, window_ns,
                    tolerance, max_age_ns, boot_id):
    result = dict(accepted=False, reason='independent_gripper_unavailable')
    if hasattr(packet, 'model_dump'):
        packet = packet.model_dump()
    if (not isinstance(packet, dict) or packet.get('source') != 'socketcan_external'
            or packet.get('protocol') != 'omnipicker_status_v1'
            or packet.get('position_unit') != 'closure_0_open_100_closed'
            or packet.get('side') != 'right' or not packet.get('running')
            or not boot_id or packet.get('boot_id') != boot_id):
        return result
    rows = []
    seen = set()
    for row in packet.get('samples', ()):
        if (not row.get('time_valid') or row.get('source_instance') != packet.get('source_instance')
                or row.get('sequence') in seen):
            return dict(result, reason='independent_gripper_sample_identity_invalid')
        seen.add(row['sequence'])
        stamp = row['received_ns'] - row['queue_delay_ns']
        if after_ns < stamp <= frame_ns:
            rows.append((stamp, float(row['closure_percent']), row['sequence']))
    if not rows or not 0 <= frame_ns - rows[-1][0] <= max_age_ns:
        return dict(result, reason='independent_gripper_not_fresh_at_image')
    # A delayed CAN queue must not turn old hardware feedback into a fresh hold.
    if any(b[0] <= a[0] or b[0] - a[0] > max_age_ns for a, b in zip(rows, rows[1:])):
        return dict(result, reason='independent_gripper_sample_gap')
    cutoff = rows[-1][0] - window_ns
    while len(rows) > 2 and rows[1][0] <= cutoff:
        rows.pop(0)
    values = np.array([r[1] for r in rows])
    result.update(source_instance=packet['source_instance'], sample_count=len(rows),
        first_sequence=rows[0][2], last_sequence=rows[-1][2],
        first_ns=rows[0][0], last_ns=rows[-1][0],
        measured_closure=float(values[-1]), span=float(np.ptp(values)),
        duration_ns=rows[-1][0]-rows[0][0], command_target=target)
    if len(rows) < 3 or rows[-1][0]-rows[0][0] < window_ns:
        return dict(result, reason='independent_gripper_window_incomplete')
    if not np.isfinite(values).all() or np.ptp(values) > tolerance:
        return dict(result, reason='independent_gripper_still_closing')
    # Quantization is a sensor property. A closed empty gripper at the commanded
    # endpoint does not demonstrate blockage by the registered sorting block.
    resolution = packet.get('position_resolution', 100/255)
    if (values.min() <= open_max or target <= open_max
            or values.max() >= target-max(tolerance, 2*resolution)):
        return dict(result, reason='independent_gripper_no_blocked_closure')
    return dict(result, accepted=True, reason='measured_blocked_closure')


class GraspClosureEpisode:
    """Bind fresh CAN decisions to one published close; a release retires that close."""

    def __init__(self):
        self.key = None
        self.source = None
        self.first_hold_ns = None
        self.retired = None

    def measure(self, observation, context, config, *, at_ns):
        contact = context.get('grasp_contact') or {}
        key = tuple(context.get(k) for k in (
            'owner_instance_id', 'session_id', 'task_id', 'task_generation', 'epoch')) + (
            contact.get('request_id'), contact.get('published_ns'), observation.host_boot_id)
        if key != self.key or not context.get('authorized'):
            self.key, self.source, self.first_hold_ns, self.retired = key, None, None, None
        packet = observation.measured_gripper
        age = config['measured_gripper_max_age_ns']
        opening = max(config['gripper_open_range'])
        position = fresh_position(packet, frame_ns=at_ns, max_age_ns=age, boot_id=observation.host_boot_id)
        open_now = position is not None and position <= opening
        if (not context.get('authorized') or not contact.get('request_id')
                or contact.get('object_id') != config['object_id'] or at_ns <= contact['published_ns']):
            return dict(held=False if open_now else None, accepted=False,
                        reason='gripper_open' if open_now else 'measured_contact_scope_or_time_mismatch')
        proof = blocked_closure(packet, frame_ns=at_ns, after_ns=contact['published_ns'],
            target=contact['target_closure'], open_max=opening, window_ns=config['stationary_window_ns'],
            tolerance=config['gripper_stationary_delta'], max_age_ns=age, boot_id=observation.host_boot_id)
        proof.update(request_id=contact['request_id'], held=None)
        if position is None:
            return proof
        samples = packet.samples
        if self.source is not None and packet.source_instance != self.source:
            self.retired = 'gripper_source_changed_after_close'
        elif self.first_hold_ns is not None and any(
                row.time_valid and self.first_hold_ns < row.received_ns-row.queue_delay_ns <= at_ns
                and row.closure_percent <= opening for row in samples):
            self.retired = 'physical_opening_after_hold'
        empty = proof['reason'] == 'independent_gripper_no_blocked_closure' and not open_now
        if empty:
            self.retired = 'measured_empty_closure'
        if self.retired:
            return dict(proof, accepted=False, held=False if open_now or empty else None, reason=self.retired)
        if proof['accepted']:
            self.source = packet.source_instance
            if self.first_hold_ns is None:
                self.first_hold_ns = proof['last_ns']
            proof['held'] = True
        elif open_now:
            proof.update(held=False, reason='gripper_open')
        return proof


def target_in_closure_region(relative, expected, box, tolerance):
    """Associate the visible target with the published grasp's contact region.

    The expected grasp defines a search region only. The actual object pose and
    blocked closure remain independently measured. Orientation is diagnostic,
    not a new acceptance threshold on SAM's coarse yaw estimate.
    """
    if not box:
        return False
    low, high = np.asarray(box['min']), np.asarray(box['max'])
    center = (low+high)/2
    actual = transform(relative)
    expected = transform(expected)
    offset = expected[:3, :3].T @ (
        actual[:3, :3]@center + actual[:3, 3] - expected[:3, 3]) - center
    return bool(np.all(np.abs(offset) <= (high-low)/2 + tolerance))
