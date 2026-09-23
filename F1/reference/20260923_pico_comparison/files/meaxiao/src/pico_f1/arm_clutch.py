"""Per-arm clutch anchoring, in hardware degrees (no ROS or motion IO)."""
from __future__ import annotations

import numpy as np


class ArmClutch:
    def __init__(self):
        self.enabled = [False, False]
        self.offset = np.zeros(14)

    def release(self, enabled):
        for side, on in enumerate(enabled):
            if not on:
                self.enabled[side] = False

    def map(self, raw, enabled, held):
        raw, held = np.asarray(raw, dtype=float), np.asarray(held, dtype=float)
        if raw.shape != (14,) or held.shape != (14,) or len(enabled) != 2:
            raise ValueError("clutch requires 14 joints and two arm enables")
        if not np.all(np.isfinite(raw)) or not np.all(np.isfinite(held)):
            raise ValueError("clutch requires finite joint positions")
        result = held.copy()
        for side, on in enumerate(enabled):
            axes = slice(side * 7, (side + 1) * 7)
            if on:
                if not self.enabled[side]:
                    self.offset[axes] = raw[axes] - held[axes]
                    # Exactly hold the first frame, including roundoff.
                else:
                    result[axes] = raw[axes] - self.offset[axes]
            self.enabled[side] = bool(on)
        return result
