"""Exact-value validation reuse cannot reuse a mutable pose or hide bad input."""
import numpy as np
import pytest

from f1_harness.geometry import transform, _validate_transform_bytes


@pytest.fixture(autouse=True)
def clear_cache():
    _validate_transform_bytes.cache_clear()
    yield
    _validate_transform_bytes.cache_clear()


def test_value_equal_inputs_reuse_validation_but_keep_caller_storage():
    first = np.eye(4); second = np.eye(4)
    assert np.shares_memory(transform(first), first)
    assert np.shares_memory(transform(second), second)
    info = _validate_transform_bytes.cache_info()
    assert info.hits == 1 and info.misses == 1
    first[0, 3] = .1
    assert transform(first)[0, 3] == .1
    assert transform(second)[0, 3] == 0.


@pytest.mark.parametrize('mutation', ['nan', 'inf', 'bottom', 'scale', 'reflection'])
def test_mutating_validated_input_cannot_hide_invalid_transform(mutation):
    value = np.eye(4); transform(value)
    if mutation == 'nan': value[0, 3] = np.nan
    elif mutation == 'inf': value[0, 3] = np.inf
    elif mutation == 'bottom': value[3, 0] = .1
    elif mutation == 'scale': value[0, 0] = 2.
    else: value[0, 0] = -1.
    with pytest.raises(ValueError): transform(value)
    assert _validate_transform_bytes.cache_info().currsize == 1


def test_invalid_input_is_rechecked_and_can_be_repaired():
    value = np.eye(4); value[0, 0] = 2.
    for _ in range(2):
        with pytest.raises(ValueError): transform(value)
    assert _validate_transform_bytes.cache_info().hits == 0
    value[0, 0] = 1.
    np.testing.assert_array_equal(transform(value), np.eye(4))


def test_stride_and_dtype_conversion_keep_original_values():
    value = np.eye(4, dtype='>f8')
    np.testing.assert_array_equal(transform(value), np.eye(4))
    view = np.eye(4).T
    assert np.shares_memory(transform(view), view)
    np.testing.assert_array_equal(transform(np.eye(4, dtype=int).reshape(-1)), np.eye(4))


def test_reuse_is_exact_even_for_sub_tolerance_pose_changes():
    a = np.eye(4); b = a.copy(); b[0, 3] = 1e-12
    transform(a); transform(b)
    assert _validate_transform_bytes.cache_info().misses == 2
    assert transform(b)[0, 3] != transform(a)[0, 3]


def test_many_frames_do_not_grow_cache_or_retain_input_arrays():
    value = np.eye(4)
    for i in range(300):
        value[0, 3] = float(i)
        transform(value)
    info = _validate_transform_bytes.cache_info()
    assert info.currsize == info.maxsize == 256
    value[0, 0] = 2.
    with pytest.raises(ValueError): transform(value)
