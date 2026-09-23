import unittest
from pico_f1.sample_history import SampleHistory


class SampleHistoryTest(unittest.TestCase):
    def test_delayed_image_selects_historical_state_not_latest_or_future(self):
        history = SampleHistory(["joint"])
        for stamp, value in [(100, 1), (120, 2), (140, 3)]:
            history.add("joint", stamp, value)
        self.assertEqual(history.at("joint", 125, 10), (2, 5))
        with self.assertRaises(ValueError):
            history.at("joint", 90, 50)
        with self.assertRaises(ValueError):
            history.at("joint", 200, 50)

    def test_capacity_and_clock_reversal_remove_invalid_history(self):
        history = SampleHistory(["joint"], capacity=2)
        for stamp in (100, 120, 140):
            history.add("joint", stamp, stamp)
        with self.assertRaises(ValueError):
            history.at("joint", 100, 1)
        history.add("joint", 50, 50)
        self.assertEqual(history.at("joint", 50, 0), (50, 0))
        self.assertEqual(len(history.values["joint"]), 1)
