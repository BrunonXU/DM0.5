"""A rejected mask update cannot move next frame's tracking prompts."""
from copy import deepcopy

import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider


def provider():
    config = dict(backend='fixed_color',
        prompts={'hole': [40, 40, 60, 60], 'anchor': [70, 40, 90, 60]},
        image_shape=[120, 120, 3], minimum_score=.9, minimum_area_px=10,
        prompt_padding_px=1, fixed_color_tracking_padding_px=20,
        maximum_overlap_fraction=.1,
        fixture_tracking_group={'primary': 'hole', 'members': ['hole', 'anchor']},
        reacquisition_boxes={'hole': [40, 20, 60, 40]},
        color_profiles={key: dict(hsv_lower=[0, 200, 200], hsv_upper=[5, 255, 255],
            minimum_area_px=20, maximum_area_px=100) for key in ('hole', 'anchor')})
    return ConfiguredMaskProvider(config, fixed_only=True)


def image(y=50, *, anchor=True):
    pixels = np.zeros((120, 120, 3), np.uint8)
    pixels[y-4:y+5, 46:55] = [255, 0, 0]
    if anchor:
        pixels[y-4:y+5, 76:85] = [255, 0, 0]
    return pixels


def test_missing_anchor_frame_does_not_poison_recovery_at_original_location():
    masks = provider()
    original = deepcopy(masks.states['default']['prompts'])
    for _ in range(3):
        with pytest.raises(ValueError, match='mask_uncertain.*anchor'):
            masks(image(30, anchor=False))
        assert masks.states['default']['prompts'] == original
    # The rejected frame's primary search moved 20 px, but the restored frame
    # must still be searched at the last accepted position, without a reset.
    assert set(masks(image())) == {'hole', 'anchor'}


def test_valid_group_translation_commits_all_prompts_once():
    masks = provider()
    original = deepcopy(masks.states['default']['prompts'])
    assert set(masks(image(30))) == {'hole', 'anchor'}
    expected = {key: [box[0], box[1]-20, box[2], box[3]-20] for key, box in original.items()}
    assert masks.states['default']['prompts'] == expected
    assert set(masks(image(30))) == {'hole', 'anchor'}
    assert masks.states['default']['prompts'] == expected


def test_late_identity_overlap_failure_does_not_commit_group_motion():
    masks = provider()
    state = masks.states['default']
    # Identity ambiguity is discovered after both masks have been computed.
    state['prompts']['anchor'] = state['prompts']['hole'].copy()
    original = deepcopy(state['prompts'])
    with pytest.raises(ValueError, match='object_identity_overlap'):
        masks(image(30, anchor=False))
    assert state['prompts'] == original


def test_segmenter_exception_after_group_relocation_cannot_commit():
    masks = provider()
    state = masks.states['default']
    original = deepcopy(state['prompts'])
    segment = state['segmenter'].segment
    def failing(*args, **kwargs):
        if kwargs.get('object_id') == 'anchor':
            raise RuntimeError('injected mask worker error')
        return segment(*args, **kwargs)
    state['segmenter'].segment = failing
    with pytest.raises(RuntimeError, match='injected'):
        masks(image(30))
    assert state['prompts'] == original
