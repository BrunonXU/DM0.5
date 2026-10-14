"""Stateful V/A/J-limited tracking of safety-approved arm waypoints.

Uses actual elapsed time, without per-packet restarts, position clipping,
or queued playback. This is not a collision checker.
"""
from __future__ import annotations

import math
import numpy as np
from pico_f1.trajectory import JerkLimitedTrajectory


class ApprovedArmOutput:
    def __init__(self, max_velocity=30., max_acceleration=75., max_jerk=300., timeout=.15):
        if any(not math.isfinite(x) or x <= 0 for x in
               (max_velocity, max_acceleration, max_jerk, timeout)):
            raise ValueError('positive finite output limits required')
        self.max_velocity, self.timeout = max_velocity, timeout
        self.planner = JerkLimitedTrajectory(14, .004, max_velocity,
            max_acceleration, max_jerk, 30., .0001)
        self.reset()

    def reset(self):
        self.position = self.target = None
        self.updated = self.sampled = None
        self.timed_out = False
        self.fault = None
        self._recoverable_timing_fault = False
        self._neutral_since = None
        self._neutral_packets = 0
        self.enabled = [False, False]

    def push(self, target, now, enabled=(True, True)):
        q = np.asarray(target, dtype=float)
        if q.shape != (14,) or not np.all(np.isfinite(q)) or not math.isfinite(now):
            raise ValueError('14 finite approved positions required')
        if len(enabled) != 2 or any(x not in (False, True) for x in enabled):
            raise ValueError('two boolean arm enables required')
        if self.position is None:
            self.position = q.copy()
            self.planner.reset(q)
            self.sampled = now
        previous_update = self.updated
        self.target, self.updated = q.copy(), now
        self.enabled = [bool(x) for x in enabled]
        mask = np.repeat(self.enabled, 7)
        self.planner.hold_axes(mask)
        self.target = np.where(mask, self.target, self.position)
        # Active packets never clear a fault. Timing faults can re-arm only
        # after a continuous disabled stream AND healthy output scheduling.
        # XR release/freshness and collision gates still apply upstream.
        if self.fault is not None:
            if (not self._recoverable_timing_fault or any(self.enabled)):
                self._neutral_since, self._neutral_packets = None, 0
            else:
                if (previous_update is None or not 0 < now-previous_update <= .05):
                    self._neutral_since, self._neutral_packets = None, 0
                if self._neutral_since is None:
                    self._neutral_since = now
                self._neutral_packets += 1

    def sample(self, now):
        if self.position is None:
            return None
        elapsed = now - self.sampled
        self.sampled = now
        if self.fault is not None:
            healthy = (math.isfinite(elapsed) and 0 <= elapsed <= .05
                       and 0 <= now-self.updated <= .05)
            if not healthy:
                self._neutral_since, self._neutral_packets = None, 0
            if (healthy and self._recoverable_timing_fault and not any(self.enabled)
                    and self._neutral_since is not None and self._neutral_packets >= 3
                    and now-self._neutral_since >= .2):
                # Never jump to feedback or replay a pending target on recovery.
                self.planner.reset(self.position)
                self.target = self.position.copy()
                self.fault = None
                self.timed_out = False
                self._recoverable_timing_fault = False
                self._neutral_since, self._neutral_packets = None, 0
                print('ARM_OUTPUT_REARMED neutral_stream_stable; position held; re-clutch to move')
            return self.position.copy()
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed > .05 or now-self.updated > self.timeout:
            self.planner.hold()
            self.target = self.position.copy()
            self.timed_out = True
            self._recoverable_timing_fault = math.isfinite(elapsed) and elapsed >= 0
            self._neutral_since, self._neutral_packets = None, 0
            self.fault = (f'target_age_ms={(now-self.updated)*1000:.1f} '
                          f'output_gap_ms={elapsed*1000:.1f}; '
                          + ('release both Grips; wait for stable disabled stream' if
                             self._recoverable_timing_fault else 'clock invalid; X+Y/reset required'))
            print(f'ARM_OUTPUT_HOLD_LATCHED {self.fault}')
            return self.position.copy()
        if elapsed == 0:
            return self.position.copy()
        try:
            proposal = self.planner.propose(self.target, dt=elapsed)
            self.planner.commit(proposal)
            self.position = np.asarray(proposal.position)
        except Exception as exc:
            self.planner.hold()
            self.fault = f'planner failure: {exc}'
            print(f'ARM_OUTPUT_HOLD_LATCHED {self.fault}')
        return self.position.copy()
