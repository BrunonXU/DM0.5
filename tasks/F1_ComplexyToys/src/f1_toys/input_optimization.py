"""Explicit right-only input optimization identities and wire validation."""
from __future__ import annotations

import hashlib
import os


MASKED_LEFT_PNG_SHA256 = 'd61b446bab0457ddc5db93d58bcffad17606477b9b4e311095d99671e82d1f46'


def environment_switch(name):
    value = os.environ.get(name, '0')
    if value not in ('0', '1'):
        raise ValueError(f'{name} must be explicitly 0 or 1')
    return value == '1'


def require_right_only(policy):
    """Reject unvalidated contracts before installing either optimization."""
    expected = dict(schema='f1_toys_eef_contract_v2', input_version='right_only_v1',
                    active_cameras=['head', 'right_wrist'],
                    cameras=['head', 'left_wrist', 'right_wrist'],
                    active_state_indices=[7, 8, 9, 10, 11, 12, 13, 15],
                    active_action_indices=list(range(10)),
                    model_action_dim=32, horizon=50, image_size=224,
                    mask_inactive_action_inputs=True)
    if any(policy.contract.get(key) != value for key, value in expected.items()):
        raise ValueError('Input optimization requires the validated right_only_v1 contract')


class MaskedLeftTransport:
    """Advertise a capability; only marked requests claim the optimization."""
    def __init__(self, policy):
        require_right_only(policy)
        digest = policy.input_preprocessing.get('contract_sha256')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Masked transport requires a bound contract SHA256')
        self.identity = dict(mode='masked_left_png_v1', constant_cameras=['left_wrist'],
                             png_sha256=MASKED_LEFT_PNG_SHA256, contract_sha256=digest)

    def validate_metadata(self, meta):
        if 'image_transport' not in meta:
            return None  # An unmarked legacy request retains its original image path.
        if meta['image_transport'] != self.identity or meta.get('prepared_pixels') is None:
            raise ValueError('Masked image transport identity mismatch')
        return dict(self.identity)

    def validate_frame(self, entry, payload):
        if entry['camera'] == 'left_wrist':
            if (entry.get('encoding') != 'image/png'
                    or hashlib.sha256(payload).hexdigest() != MASKED_LEFT_PNG_SHA256):
                raise ValueError('Masked left PNG bytes do not match the advertised constant')


def configure_image_transport(policy):
    if not environment_switch('F1_MASKED_LEFT_TRANSPORT'):
        return None
    return MaskedLeftTransport(policy)
