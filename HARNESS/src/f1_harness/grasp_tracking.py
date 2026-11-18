"""Measured following evidence from the current contiguous grasp segment.

Closing/reseating samples may precede a valid lift. They cannot poison the entire
history, and cannot be cherry-picked through a later incompatible measurement.
This module neither commands a probe motion nor treats gripper closure as a grasp.
"""
import math

import numpy as np

from .geometry import align_square_pose


def square_rotation_error(a, b):
    aligned, _ = align_square_pose(a, b)
    return math.acos(float(np.clip((np.trace(aligned[:3, :3] @ b[:3, :3].T) - 1) / 2, -1, 1)))


def following_evidence(history, *, translation_tolerance, rotation_tolerance, gripper_tolerance=None):
    """Evaluate the longest compatible suffix ending at the actual latest sample.

Thresholds are the task contract. rotation_tolerance=None requests positional
attachment evidence and reports orientation variation without rejecting it.
This is not a claim that the in-hand orientation is precise. Three independent
camera/feedback pairs and actual EEF displacement are required; fresh static
frames alone never establish a grasp. An incompatible old prefix is excluded,
but an incompatible latest frame cannot be skipped in favor of an earlier lift.
    """
    rows = list(history)
    result = dict(accepted=False, support_samples=0, segment_samples=0, motion_m=0.,
                  relative_translation_m=0., relative_rotation_rad=0., gripper_span=0.,
                  boundary_reason=None, reason='no_tracking_samples')
    if not rows:
        return result
    latest = rows[-1]
    supported = []
    relative_metrics = []
    seen_frames, seen_states = set(), set()
    low = high = latest['gripper']
    last_time = last_stamps = None
    segment_samples = 0
    for row in reversed(rows):
        if row['camera_identity'] != latest['camera_identity']:
            result['boundary_reason'] = 'camera_identity_changed'
            break
        translation = float(np.linalg.norm(row['relative'][:3, 3] - latest['relative'][:3, 3]))
        rotation = square_rotation_error(row['relative'], latest['relative'])
        next_low, next_high = min(low, row['gripper']), max(high, row['gripper'])
        boundary = ('relative_translation_changed' if translation >= translation_tolerance else
                    'relative_rotation_changed' if rotation_tolerance is not None and rotation >= rotation_tolerance else
                    'gripper_not_stable' if gripper_tolerance is not None
                    and next_high - next_low >= gripper_tolerance else None)
        if boundary:
            result['boundary_reason'] = boundary
            result['boundary_metrics'] = dict(relative_translation_m=translation,
                                              relative_rotation_rad=rotation,
                                              gripper_span=float(next_high - next_low))
            break
        stamps = tuple(row['source_stamps'])
        if any(stamp is None for stamp in stamps):
            result['boundary_reason'] = 'source_time_missing'
            break
        if not isinstance(row['state_ns'], (int, np.integer)) or isinstance(row['state_ns'], bool):
            result['boundary_reason'] = 'feedback_time_missing'
            break
        frame_key = (row['camera_identity'], stamps)
        # A wrapper/sequence change around the same source capture is not an
        # independent frame, nor is a new image paired to the same feedback.
        independent = frame_key not in seen_frames and row['state_ns'] not in seen_states
        if last_time is not None and row['time'] > last_time:
            result['boundary_reason'] = 'sample_time_reversed'
            break
        if independent and last_stamps is not None and any(a >= b for a, b in zip(stamps, last_stamps)):
            result['boundary_reason'] = 'source_time_not_increasing'
            break
        low, high = next_low, next_high
        segment_samples += 1
        if independent:
            supported.append(row)
            relative_metrics.append((translation, rotation))
            seen_frames.add(frame_key)
            seen_states.add(row['state_ns'])
            last_time, last_stamps = row['time'], stamps
    motion = max((float(np.linalg.norm(row['eef'][:3, 3] - latest['eef'][:3, 3]))
                  for row in supported), default=0.)
    relative_translation = max((row[0] for row in relative_metrics), default=0.)
    relative_rotation = max((row[1] for row in relative_metrics), default=0.)
    result.update(support_samples=len(supported), segment_samples=segment_samples, motion_m=motion,
                  relative_translation_m=relative_translation, relative_rotation_rad=relative_rotation,
                  gripper_span=float(high - low))
    if len(supported) < 3:
        result['reason'] = 'insufficient_independent_following_samples'
    elif motion <= 2 * translation_tolerance:
        result['reason'] = 'insufficient_following_motion'
    else:
        result.update(accepted=True, reason='measured_contiguous_following')
    return result


def nonfollowing_evidence(history, *, translation_tolerance, gripper_tolerance):
    """Prove a visible object stayed in world space while the closed hand moved.

    Use the same contiguous independent measurements as positive following.
    No mask, no actual hand displacement, or a moving gripper leaves this
    unknown. This is positional non-attachment, never a force measurement.
    """
    if gripper_tolerance is None:
        return dict(accepted=False,reason='gripper_stability_contract_missing',support_samples=0)
    stationary_object = [dict(row, relative=row['obj']) for row in history]
    result = following_evidence(stationary_object,
        translation_tolerance=translation_tolerance, rotation_tolerance=None,
        gripper_tolerance=gripper_tolerance)
    result['object_world_displacement_m'] = result.pop('relative_translation_m')
    result.pop('relative_rotation_rad')
    if result['accepted']:
        result['reason'] = 'measured_object_did_not_follow_hand'
    return result


def quarantine_following(reference, gripper, sample, *, expires_ns):
    """Isolate a previously measured attachment without making a current claim."""
    return dict(reference=reference.copy(), gripper=gripper, camera_identity=sample['camera_identity'],
                scope_identity=sample.get('scope_identity'), started_ns=sample['time'],
                expires_ns=expires_ns, source_stamps=sample['source_stamps'],
                state_ns=sample['state_ns'], support=[], departures=[])


def handoff_reference(reference, camera_identity):
    """Suspend historical proof while an associated new view remeasures it.

    The scene pipeline has already checked calibration, object association and
    scope. This transfers no current holding claim and never changes the old
    proof's deadline or source stamps. Camera source clocks are independent;
    subsequent freshness uses common receipt/feedback time and monotonic source
    stamps within the new view. Cross-view samples never create following.
    """
    return reference | dict(revalidation_camera_identity=camera_identity,
                            suspended=True,support=[],departures=[])


def separated_attachment(reference, measured, object_box, tolerance):
    """Measured box is outside the old grasped volume, including measurement error.

    Six box-face axes give a conservative separation test. No separation is
    inconclusive (not proof of holding); a partial slip/rotation is not a miss.
    The registered object dimensions and existing tracking error are reused.
    """
    from itertools import product
    corners=np.asarray(list(product(*zip(object_box['min'],object_box['max']))),float)
    old=corners@reference[:3,:3].T+reference[:3,3]
    new=corners@measured[:3,:3].T+measured[:3,3]
    for axis in (*reference[:3,:3].T,*measured[:3,:3].T):
        a,b=old@axis,new@axis
        if max(float(b.min()-a.max()),float(a.min()-b.max()))>tolerance:
            return True
    return False


def revalidate_nonfollowing(reference, history, *, object_box, translation_tolerance,
                           gripper_tolerance):
    """Reconcile a historical miss with the current measured object, not a score.

    A single noisy pose suspends the current claim without erasing the original
    nonfollowing proof. Reuse the existing independent-sample contract to check
    a stable world-space suffix, and associate it with the registered object
    volume. A different object, grasp opening or scope cannot inherit the proof.
    This does not infer empty fingers from unknown attachment evidence.
    """
    rows = list(history)
    result = dict(restored=False, discard=False, reason='miss_evidence_pending')
    if not rows or object_box is None or gripper_tolerance is None:
        return result
    latest = rows[-1]
    camera=reference.get('revalidation_camera_identity',reference.get('camera_identity'))
    if (camera != latest['camera_identity']
            or reference.get('scope_identity') != latest.get('scope_identity')
            or abs(reference['gripper'] - latest['gripper']) >= gripper_tolerance):
        return result | dict(discard=True, reason='miss_evidence_scope_changed')
    fresh = [row for row in rows if row['time'] > reference.get('time', row['time'])
             and row['state_ns'] > reference.get('state_ns', row['state_ns'])
             and row.get('scope_identity') == reference.get('scope_identity')
             and row['camera_identity']==camera
             and all(a is not None for a in row['source_stamps'])
             and (camera!=reference.get('camera_identity') or
                  all(a > b for a, b in zip(row['source_stamps'], reference.get('source_stamps', row['source_stamps']))))]
    stable = following_evidence([dict(row, relative=row['obj']) for row in fresh],
        translation_tolerance=translation_tolerance, rotation_tolerance=None,
        gripper_tolerance=gripper_tolerance)
    result.update(support_samples=stable['support_samples'],
                  object_displacement_m=stable['relative_translation_m'])
    if separated_attachment(reference['obj'], latest['obj'], object_box, translation_tolerance):
        return result | dict(reason='miss_object_association_unresolved')
    # Three independent pairs are already required by following/nonfollowing;
    # here no new hand motion is needed to repeat an established miss proof.
    if stable['support_samples'] < 3:
        return result
    return result | dict(restored=True, reason='measured_nonfollowing_revalidated')


def revalidate_following(proof, sample, *, translation_tolerance, rotation_tolerance, gripper_tolerance=None,
                        object_box=None):
    """Revalidate the unchanged original proof using three new matching pairs.

The deadline belongs to the isolated proof and is never extended by retries.
Static samples can revalidate an earlier following proof, but never create one.
The caller clears the proof on opening, lost feedback, or owner scope changes.
    """
    result = dict(restored=False, discard=False, quarantine_started_ns=proof['started_ns'],
                  quarantine_expires_ns=proof['expires_ns'], quarantine_support_samples=len(proof['support']))
    if proof['expires_ns'] is not None and sample['time'] >= proof['expires_ns']:
        return result | dict(discard=True, quarantine_state='expired', reason='following_quarantine_expired')
    camera=proof.get('revalidation_camera_identity',proof['camera_identity'])
    if (sample['camera_identity'] != camera
            or sample.get('scope_identity') != proof.get('scope_identity')):
        return result | dict(discard=True, quarantine_state='invalidated', reason='following_quarantine_scope_changed')
    if not isinstance(sample['state_ns'], (int, np.integer)) or isinstance(sample['state_ns'], bool):
        return result | dict(discard=True, quarantine_state='invalidated', reason='following_quarantine_feedback_missing')
    if gripper_tolerance is not None and abs(sample['gripper'] - proof['gripper']) >= gripper_tolerance:
        return result | dict(discard=True, quarantine_state='invalidated', reason='gripper_changed_after_following')
    translation = float(np.linalg.norm(sample['relative'][:3, 3] - proof['reference'][:3, 3]))
    rotation = square_rotation_error(sample['relative'], proof['reference'])
    result.update(reference_translation_m=translation, reference_rotation_rad=rotation)
    stamps = tuple(sample['source_stamps'])
    current_sources=all(a is not None for a in stamps)
    newer_sources=(current_sources and (camera!=proof['camera_identity'] or
                    all(b is not None and a>b for a,b in zip(stamps,proof['source_stamps']))))
    independent=(sample['time']>proof['started_ns'] and sample['state_ns']>proof['state_ns'] and newer_sources)
    if translation >= translation_tolerance or rotation_tolerance is not None and rotation >= rotation_tolerance:
        proof['support'].clear()
        departures=proof.setdefault('departures',[])
        if object_box is not None and separated_attachment(proof['reference'],sample['relative'],object_box,translation_tolerance):
            if independent and (not departures or sample['time']>departures[-1]['time']
                    and sample['state_ns']>departures[-1]['state_ns']
                    and all(a>b for a,b in zip(stamps,departures[-1]['source_stamps']))):
                departures.append(dict(time=sample['time'],state_ns=sample['state_ns'],source_stamps=stamps))
                del departures[:-3]
        else:departures.clear()
        return result | dict(quarantine_support_samples=0, quarantine_state='pending',
                             detached=len(departures)>=3, departure_samples=len(departures),
                             reason='measured_attachment_departed' if len(departures)>=3 else
                                    'following_quarantine_measurement_mismatch')
    proof.setdefault('departures',[]).clear()
    if not independent:
        return result | dict(quarantine_state='pending', reason='following_quarantine_measurement_not_fresh')
    support = proof['support']
    if support and (sample['time'] <= support[-1]['time'] or sample['state_ns'] <= support[-1]['state_ns']
                    or any(a <= b for a, b in zip(stamps, support[-1]['source_stamps']))):
        return result | dict(quarantine_state='pending', reason='following_quarantine_measurement_not_independent')
    support.append(dict(time=sample['time'], source_stamps=stamps, state_ns=sample['state_ns']))
    restored = len(support) >= 3
    return result | dict(restored=restored, quarantine_support_samples=len(support),
                         quarantine_state='restored' if restored else 'pending',
                         reason='measured_following_revalidated' if restored else 'following_quarantine_waiting_samples')
