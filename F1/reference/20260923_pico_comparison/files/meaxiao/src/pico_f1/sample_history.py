"""Bounded, causal history for aligning feedback with delayed camera frames."""
from collections import deque


class SampleHistory:
    def __init__(self, keys, capacity=8192):
        self.values = {key: deque(maxlen=capacity) for key in keys}

    def add(self, key, timestamp_ns, value):
        history = self.values[key]
        if history and timestamp_ns < history[-1][0]:
            history.clear()
        history.append((int(timestamp_ns), float(value)))

    def at(self, key, timestamp_ns, max_age_ns):
        for stamp, value in reversed(self.values[key]):
            if stamp <= timestamp_ns:
                age = timestamp_ns - stamp
                if age <= max_age_ns:
                    return value, age
                break
        raise ValueError(f"no aligned history for {key}")
