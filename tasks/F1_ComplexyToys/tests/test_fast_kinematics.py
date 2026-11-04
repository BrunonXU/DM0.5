from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from f1_toys.native import kinematics
from f1_toys.native.fast_kinematics import FastArmModel


def test_math_exact_at_limits_and_concurrent_calls():
    config = Path(kinematics.__file__).with_name('ccs_m6_40.MvKDCfg')
    original, candidate = kinematics.ArmModel(config), FastArmModel(config)
    rng = np.random.default_rng(20)
    joints = [original.low, original.high, np.zeros(7)]
    joints.extend(rng.uniform(original.low, original.high, size=(40, 7)))

    def check(pair):
        arm, q = pair
        target = original.fk_rad(arm, np.zeros(7))
        for method, args in (
            ('fk_rad', (arm, q)), ('residual', (arm, q, target)),
        ):
            a = getattr(original, method)(*args)
            b = getattr(candidate, method)(*args)
            if method == 'residual':
                for x, y in zip(a, b):
                    np.testing.assert_array_equal(x, y)
            else:
                np.testing.assert_array_equal(a, b)
        for x, y in zip(original.fk_rad(arm, q, jacobian=True),
                        candidate.fk_rad(arm, q, jacobian=True)):
            np.testing.assert_array_equal(x, y)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(check, [(arm, q) for arm in (0, 1) for q in joints]))
    for q in ([np.nan]*7, [0]*6):
        with pytest.raises(ValueError):
            candidate.fk_rad(1, q)
    with pytest.raises(ValueError):
        candidate.fk_rad(2, np.zeros(7))
