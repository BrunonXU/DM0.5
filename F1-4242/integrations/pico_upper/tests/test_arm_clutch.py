import unittest

import numpy as np

from pico_f1.arm_clutch import ArmClutch


class ArmClutchTest(unittest.TestCase):
    def test_first_grip_discards_idle_ik_offset_on_each_arm(self):
        for enabled in ([True, False], [False, True], [True, True]):
            clutch = ArmClutch()
            held = np.arange(14.)
            raw = held + np.linspace(-80., 80., 14)
            np.testing.assert_array_equal(clutch.map(raw, enabled, held), held)
            np.testing.assert_allclose(clutch.map(raw, enabled, held), held, atol=1e-14)
            result = clutch.map(raw + 0.2, enabled, held)
            expected = held + np.repeat(enabled, 7) * .2
            np.testing.assert_allclose(result, expected)

    def test_regrip_does_not_change_other_active_arm(self):
        clutch = ArmClutch()
        held = np.zeros(14)
        clutch.map(np.ones(14) * 20, [True, True], held)
        clutch.release([False, True])
        raw = np.r_[np.ones(7) * 70, np.ones(7) * 21]
        result = clutch.map(raw, [True, True], held)
        np.testing.assert_array_equal(result[:7], held[:7])
        np.testing.assert_array_equal(result[7:], np.ones(7))

    def test_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            ArmClutch().map([float('nan')] * 14, [True, False], [0.] * 14)
