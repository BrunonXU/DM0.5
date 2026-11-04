"""Negotiated sample-after-challenge proof; no robot or clock-sync assumptions."""
from dataclasses import replace


def recovery_token(ack):
    if ack is None or not ack.accepted:
        return None
    for field in (ack.reason or '').split(';'):
        if field.startswith('recovery_token='):
            token = field.split('=', 1)[1]
            if len(token) == 32 and all(c in '0123456789abcdef' for c in token):
                return token
    return None


def without_recovery_token(reason):
    return ';'.join(f for f in (reason or '').split(';') if not f.startswith('recovery_token='))


def sampled_recovery_packet(packet, token_before_sample, sample, neutral_threshold):
    """Call only with an ACK read BEFORE polling the physical controller.

    Never attach a newly received token to a queued/previous sample. Neutral
    comes from raw controls, not axes zeroed by the mapper when RB is released.
    """
    if token_before_sample is None:
        return packet
    neutral = (sample.connected and not any((sample.rb, sample.lb, sample.a,
        sample.b, sample.x, sample.y, sample.start, sample.dpad_x, sample.dpad_y))
        and max(abs(sample.left_x), abs(sample.left_y), abs(sample.right_x),
                abs(sample.right_y), abs(sample.left_trigger), abs(sample.right_trigger)) <= neutral_threshold)
    return replace(packet, recovery_token=token_before_sample, controls_neutral=neutral)
