"""Bounded receiver-clock challenge history for explicit RB recovery."""
from collections import OrderedDict, deque
import secrets
import threading


class RecoveryChallenges:
    def __init__(self, capacity=128):
        self.lock = threading.Lock()
        self.tokens = OrderedDict()
        self.capacity = capacity

    def issue(self, session, now):
        token = secrets.token_hex(16)
        with self.lock:
            self.tokens[token] = (session, now)
            while len(self.tokens) > self.capacity:
                self.tokens.popitem(last=False)
        return token

    def age(self, packet, after_ns, now, budget_ns):
        with self.lock:
            entry = self.tokens.get(packet.recovery_token)
        if entry is None:
            return None
        session, issued = entry
        if session != packet.session_id or not after_ns <= issued <= now:
            return None
        age = now - issued
        return age if age <= budget_ns else None


class InputTiming:
    """Clock-local intervals and sequence gaps, never one-way latency."""
    def __init__(self):
        self.lock = threading.Lock()
        self.previous = None
        self.gaps = deque(maxlen=500)
        self.queue = deque(maxlen=500)
        self.sender_intervals = deque(maxlen=500)
        self.sequence_gaps = deque(maxlen=500)

    def observe(self, packet, received, now):
        with self.lock:
            if self.previous and self.previous[0] == packet.session_id:
                self.gaps.append((received-self.previous[1])/1e6)
                gap = packet.sequence-self.previous[2]
                self.sequence_gaps.append(gap)
                elapsed = packet.sent_at_unix_ns-self.previous[3]
                if gap > 0 and elapsed >= 0:
                    # Latest-slot consumption can skip packets. Divide by the
                    # sequence span; do not label that skip as network loss.
                    self.sender_intervals.append(elapsed/gap/1e6)
            self.previous = (packet.session_id, received, packet.sequence, packet.sent_at_unix_ns)
            self.queue.append((now-received)/1e6)

    def snapshot(self):
        def stats(values):
            a = sorted(values)
            if not a:
                return dict(count=0)
            return dict(count=len(a), mean=sum(a)/len(a), p50=a[(len(a)-1)//2],
                        p99=a[min(len(a)-1, int(len(a)*.99))], maximum=a[-1])
        with self.lock:
            return dict(received_gap_ms=stats(self.gaps), receive_to_consume_ms=stats(self.queue),
                        sender_ms_per_sequence=stats(self.sender_intervals),
                        consumed_sequence_gap=stats(self.sequence_gaps))
