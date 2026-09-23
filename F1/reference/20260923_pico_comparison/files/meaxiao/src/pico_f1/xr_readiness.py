"""Pure readiness state: cached nonzero poses are NOT proof of a live stream."""
import math


def valid_pose(pose):
    try:
        return (len(pose) == 7 and all(math.isfinite(float(v)) for v in pose)
                and .5 < sum(float(v)**2 for v in pose[3:])**.5 < 1.5)
    except (TypeError, ValueError):
        return False


class XrReadiness:
    def __init__(self, timeout=.25, neutral_seconds=.2):
        self.timeout = timeout
        self.neutral_seconds = neutral_seconds
        self.timestamp = None
        self.changed_at = None
        self.updates = 0
        self.neutral_since = None
        self.ready = False
        self.reason = 'WAIT_XR'
        self.last_fault = 'none'
        self.last_fault_age_s = 0.

    def update(self, timestamp, poses, neutral, now):
        valid = timestamp > 0 and len(poses) == 3 and all(valid_pose(p) for p in poses)
        rollback = self.timestamp is not None and timestamp < self.timestamp
        expired = self.changed_at is not None and now - self.changed_at > self.timeout
        if not valid or rollback or expired:
            self.last_fault = ('invalid_timestamp_or_pose' if not valid else
                               'timestamp_rollback' if rollback else 'timestamp_stalled')
            self.last_fault_age_s = 0. if self.changed_at is None else now-self.changed_at
            self.ready = False
            self.updates = 0
            self.neutral_since = None
        if valid and timestamp != self.timestamp:
            self.updates += 1
            self.changed_at = now
        self.timestamp = timestamp
        fresh = valid and self.changed_at is not None and now - self.changed_at <= self.timeout
        if not fresh:
            self.ready = False
            self.reason = 'WAIT_XR_FRESH_FRAMES'
            return False
        if not self.ready:
            if not neutral:
                self.neutral_since = None
            elif self.neutral_since is None:
                self.neutral_since = now
            if (self.updates >= 3 and self.neutral_since is not None
                    and now - self.neutral_since >= self.neutral_seconds):
                self.ready = True
            self.reason = 'XR_READY' if self.ready else 'WAIT_XR_RELEASE_CONTROLS'
        return self.ready
