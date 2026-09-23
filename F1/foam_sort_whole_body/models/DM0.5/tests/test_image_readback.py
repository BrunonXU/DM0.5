import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_data.image_readback import check_camera_selection


def test_causal_selection_includes_last_equal_timestamp():
    age = check_camera_selection(np.array([10, 25, 30]), np.array([0, 2, 3]),
                                 np.array([10, 20, 20, 30]), np.ones(4, dtype=bool))
    np.testing.assert_array_equal(age, [0, 5, 0])


@pytest.mark.parametrize("selected", [[0, 0], [1, 1]])
def test_rejects_stale_or_future_selection(selected):
    with pytest.raises(ValueError, match="latest causal"):
        check_camera_selection(np.array([15, 25]), np.array(selected),
                               np.array([10, 20]), np.ones(2, dtype=bool))


def test_rejects_original_timestamp_flag():
    with pytest.raises(ValueError, match="latest causal"):
        check_camera_selection(np.array([25]), np.array([1]), np.array([10, 20]),
                               np.array([True, False]))


def test_skips_newer_frame_with_invalid_clock():
    age = check_camera_selection(np.array([25]), np.array([0]), np.array([10, 20]),
                                 np.array([True, False]))
    np.testing.assert_array_equal(age, [15])


def test_does_not_wrap_negative_index_before_first_valid_frame():
    with pytest.raises(ValueError, match="precedes valid"):
        check_camera_selection(np.array([5]), np.array([-1]), np.array([10, 20]),
                               np.array([True, True]))
