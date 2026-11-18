"""Offline wall-clock pacing and a real asynchronous planner executor.

No device/transport/model interfaces. Numeric service instances must be private
to their worker: native FK/collision data is mutable and not thread-safe.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time


class PacedClock:
    def __init__(self, *, origin_ns, period_ns=20_000_000, max_seconds=60.):
        self.origin_ns, self.period_ns, self.max_seconds = origin_ns, period_ns, max_seconds
        self.start_ns = None
        self.last_ns = origin_ns
        self.intervals_ns = []

    def advance(self):
        if self.start_ns is None:
            self.start_ns = time.monotonic_ns()
        due = self.start_ns + self.last_ns-self.origin_ns + self.period_ns
        remaining = (due-time.monotonic_ns())/1e9
        if remaining > 0:
            time.sleep(remaining)
        now = self.origin_ns + time.monotonic_ns()-self.start_ns
        if now-self.origin_ns > self.max_seconds*1e9:
            raise TimeoutError('offline_wall_clock_run_budget_exceeded')
        self.intervals_ns.append(now-self.last_ns)
        self.last_ns = now
        return now


class AsyncReplayExecutor:
    """Use the same one-worker scheduling as OwnerBridge, instrumented offline."""
    def __init__(self, records, *, plan_delay_s=0.):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='offline-plan')
        self.records, self.plan_delay_s = records, plan_delay_s
        self.active = set()
        self.lock = threading.Lock()

    def submit(self, function, *args):
        from f1_harness.contracts import SkillIntent
        intents = [value for value in args if isinstance(value, SkillIntent)]
        if len(intents) != 1:
            raise RuntimeError('simulation_computation_intent_ambiguous')
        intent = intents[0]
        queued = time.monotonic_ns()

        def work():
            begin = time.monotonic_ns()
            with self.lock:
                self.active.add((intent.request_id, function.__name__))
            error = None
            try:
                if function.__name__ == 'plan' and self.plan_delay_s:
                    time.sleep(self.plan_delay_s)
                return function(*args)
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                end = time.monotonic_ns()
                with self.lock:
                    self.records.append(dict(function=function.__name__, wall_s=(end-begin)/1e9,
                        queued_wall_ns=queued, started_wall_ns=begin, ended_wall_ns=end,
                        request_id=intent.request_id, skill=intent.skill, error_type=error,
                        injected_plan_delay_s=self.plan_delay_s if function.__name__=='plan' else 0.))
                    self.active.discard((intent.request_id, function.__name__))
        return self.pool.submit(work)

    def pump(self):
        pass  # Work happens on its own thread; the control clock never pauses.

    def active_jobs(self):
        with self.lock:
            return [dict(request_id=r, function=f) for r, f in sorted(self.active)]

    def shutdown(self, **kwargs):
        self.pool.shutdown(**kwargs)
