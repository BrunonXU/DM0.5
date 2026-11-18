"""A clipped search prompt can grow; missing or ambiguous identities cannot."""
from copy import deepcopy

import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.perception import FixedColorSegmenter


def config(box=(20, 20, 40, 40), *, padding=4, maximum_area=1000):
    return dict(backend='fixed_color', image_shape=[80, 80, 3],
        prompts={'block': list(box)}, minimum_score=.9, minimum_area_px=10,
        prompt_padding_px=1, fixed_color_tracking_padding_px=padding,
        maximum_overlap_fraction=.1, color_profiles={'block': dict(
            hsv_lower=[0, 200, 200], hsv_upper=[5, 255, 255],
            minimum_area_px=10, maximum_area_px=maximum_area)})


def image(*rectangles):
    rgb = np.zeros((80, 80, 3), np.uint8)
    for x0, y0, x1, y1 in rectangles:
        rgb[y0:y1, x0:x1] = [255, 0, 0]
    return rgb


@pytest.mark.parametrize('rectangle', [
    (17, 25, 30, 35), (30, 25, 43, 35),
    (25, 17, 35, 30), (25, 30, 35, 43), (17, 17, 30, 30),
    (30, 25, 69, 35),  # More than one progressive expansion is necessary.
])
def test_clipped_search_is_recovered_without_changing_strict_segment(rectangle):
    cfg = config(); rgb = image(rectangle)
    strict = FixedColorSegmenter(cfg['color_profiles'])
    assert strict.segment(rgb, box=cfg['prompts']['block'], object_id='block')[0][1] == 0
    mask = ConfiguredMaskProvider(cfg, fixed_only=True)(rgb)['block']
    assert np.array_equal(mask, rgb[:, :, 0] > 0)


@pytest.mark.parametrize('rectangles', [
    [(0, 25, 30, 35)],  # Real image clipping cannot be repaired.
    [(30, 25, 80, 35)],
    [(17, 24, 30, 28), (25, 33, 35, 37)],  # Ambiguous original prompt.
    [(30, 23, 43, 28), (39, 33, 50, 38)],  # Second seed fragment grows past min area.
    [(45, 25, 55, 35)],  # No seed: never jump to a new object elsewhere.
])
def test_clipped_recovery_keeps_unavailable_cases_unavailable(rectangles):
    masks = ConfiguredMaskProvider(config(), fixed_only=True)
    before = deepcopy(masks.tracking_snapshot())
    with pytest.raises(ValueError, match='mask_uncertain'):
        masks(image(*rectangles))
    assert masks.tracking_snapshot() == before


def test_full_component_must_still_satisfy_registered_area():
    masks = ConfiguredMaskProvider(config(maximum_area=150), fixed_only=True)
    with pytest.raises(ValueError, match='mask_uncertain'):
        masks(image((30, 25, 60, 35)))


def test_seed_identity_does_not_switch_to_bigger_outside_component():
    cfg = config(padding=20)
    rgb = image((30, 25, 43, 35), (45, 15, 58, 45))
    mask = ConfiguredMaskProvider(cfg, fixed_only=True)(rgb)['block']
    expected = image((30, 25, 43, 35))[:, :, 0] > 0
    assert np.array_equal(mask, expected)


def test_recovered_prompt_only_commits_after_geometry_accepts_the_view():
    masks = ConfiguredMaskProvider(config(), fixed_only=True)
    before = deepcopy(masks.tracking_snapshot())
    token = masks.begin_tracking_frame()
    assert masks(image((30, 25, 43, 35)))['block'].sum() == 130
    assert masks.tracking_snapshot() == before
    masks.finish_tracking_frame(token, accepted_views=[])
    assert masks.tracking_snapshot() == before
    token = masks.begin_tracking_frame()
    masks(image((30, 25, 43, 35)))
    masks.finish_tracking_frame(token, accepted_views=['default'])
    assert masks.tracking_snapshot() != before


def test_explicit_search_hint_can_recover_a_clipped_unique_seed():
    masks = ConfiguredMaskProvider(config(box=(2, 2, 10, 10)), fixed_only=True)
    rgb = image((30, 25, 43, 35))
    result = masks.with_search_boxes(rgb, view='default', boxes={'block': [20, 20, 40, 40]})
    assert np.array_equal(result['block'], rgb[:, :, 0] > 0)


def test_explicit_reseed_replaces_valid_old_blob_prompt_before_next_frame():
    cfg = config(box=(10, 10, 30, 30))
    masks = ConfiguredMaskProvider(cfg, fixed_only=True)
    rgb = image((15, 15, 25, 25), (45, 45, 55, 55))
    old = masks(rgb)['block']
    before_config = deepcopy(masks.states['default']['config'])
    masks.set_search_hint(view='default', object_id='block', box=[40, 40, 60, 60])
    new = masks(rgb)['block']
    assert old[20, 20] and not old[50, 50]
    assert new[50, 50] and not new[20, 20]
    assert masks.states['default']['config'] == before_config


@pytest.mark.parametrize('kwargs', [
    dict(view='missing', object_id='block', box=[10, 10, 30, 30]),
    dict(view='default', object_id='missing', box=[10, 10, 30, 30]),
    dict(view='default', object_id='block', box=[10., 10, 30, 30]),
    dict(view='default', object_id='block', box=[True, 10, 30, 30]),
    dict(view='default', object_id='block', box=[-1, 10, 30, 30]),
    dict(view='default', object_id='block', box=[10, 10, 81, 30]),
    dict(view='default', object_id='block', box=[30, 10, 10, 30]),
])
def test_explicit_reseed_rejects_bad_identity_or_box_without_mutation(kwargs):
    masks = ConfiguredMaskProvider(config(), fixed_only=True)
    before = masks.tracking_snapshot()
    with pytest.raises(ValueError):
        masks.set_search_hint(**kwargs)
    assert masks.tracking_snapshot() == before


def test_explicit_reseed_cannot_race_active_geometry_transaction():
    masks = ConfiguredMaskProvider(config(), fixed_only=True)
    before = masks.tracking_snapshot()
    token = masks.begin_tracking_frame()
    with pytest.raises(ValueError, match='mask_tracking_frame_already_active'):
        masks.set_search_hint(view='default', object_id='block', box=[40, 40, 60, 60])
    masks.finish_tracking_frame(token, accepted_views=[])
    assert masks.tracking_snapshot() == before
