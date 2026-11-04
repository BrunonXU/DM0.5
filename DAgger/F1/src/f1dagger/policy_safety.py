"""Worker-owned plan screening; no command publisher or shared native data."""
import time
import numpy as np


class PolicyPlanGuard:
    def __init__(self, gate, mapping, emit):
        self.gate, self.mapping, self.emit = gate, mapping, emit

    def __call__(self, observation, actions, epoch, request_id, *, committed_prefix=0,
                 max_new_waypoints=None):
        started = time.monotonic_ns()
        previous = np.asarray(observation.state16).copy()
        if not 0 <= committed_prefix < len(actions):
            raise ValueError('Invalid committed policy prefix')
        if committed_prefix:
            previous = np.asarray(actions[committed_prefix-1]).copy()
        remaining = len(actions)-committed_prefix
        if max_new_waypoints is not None and (not isinstance(max_new_waypoints,int) or max_new_waypoints < 1):
            raise ValueError('Invalid plan screening horizon')
        checked_waypoints = remaining if max_new_waypoints is None else min(remaining,max_new_waypoints)
        count = 0
        try:
            for row in actions[committed_prefix:committed_prefix+checked_waypoints]:
                target = np.asarray(row).copy()
                target[:7], target[14] = previous[:7], previous[14]
                for value in ((previous+target)*.5, target):
                    self.mapping.check_action(value)
                    self.gate.check_preventive(value, observation.full22)
                    count += 1
                previous = target
        except Exception as exc:
            self.emit('policy_path_rejected', request_id=request_id, policy_epoch=epoch,
                      checked_samples=count, reason=str(exc), proposal_only=True,
                      elapsed_ms=(time.monotonic_ns()-started)/1e6)
            raise
        self.emit('policy_path_checked', request_id=request_id, policy_epoch=epoch,
                  checked_samples=count, proposal_only=True,
                  previously_checked_committed_prefix=committed_prefix,
                  screened_new_waypoints=checked_waypoints,
                  deferred_new_waypoints=remaining-checked_waypoints,
                  elapsed_ms=(time.monotonic_ns()-started)/1e6,
                  scope=('sampled_waypoints_and_midpoints_not_continuous_collision_proof'
                         if checked_waypoints==remaining else
                         'rtc_join_only_remaining_targets_and_smoothed_commands_checked_before_publication'))


class ProcessPolicyPlanGuard:
    """One spawn-owned guard; blocking IPC runs only in the inference thread.

    Native guard state and ROS objects never cross the process boundary. A
    timed-out/dead worker is permanently closed, not silently replaced while
    control is running. Every returned plan requires its matching full receipt.
    """
    def __init__(self, gate_spec, emit, *, startup_timeout_s=45., request_timeout_s=2.,
                 factory='f1dagger.robot_io:collision_gate_from_spec', stop_requested=lambda: False):
        import multiprocessing
        import threading
        if (not np.isfinite([startup_timeout_s, request_timeout_s]).all()
                or startup_timeout_s <= 0 or request_timeout_s <= 0):
            raise ValueError('positive finite guard process timeouts required')
        self.emit, self.request_timeout_s = emit, float(request_timeout_s)
        self._stop_requested = stop_requested
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = threading.Event()
        self._sequence = 0
        context = multiprocessing.get_context('spawn')
        self._connection, child = context.Pipe(duplex=True)
        self._process = context.Process(target=_guard_process_main,
            args=(child, gate_spec, factory), name='f1-policy-plan-guard', daemon=True)
        try:
            self._process.start()
            child.close()
            message = self._receive(startup_timeout_s)
            if message.get('kind') != 'ready':
                raise RuntimeError('plan guard initialization failed: ' + str(message.get('error', message)))
        except BaseException:
            child.close()
            self.close()
            raise

    def _receive(self, timeout):
        deadline = time.monotonic() + timeout
        while not self._closed.is_set():
            if self._stop_requested():
                raise RuntimeError('plan guard shutdown requested')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError('plan guard worker timeout')
            try:
                if self._connection.poll(min(.02, remaining)):
                    message = self._connection.recv()
                    if self._closed.is_set():
                        raise RuntimeError('plan guard worker closed')
                    if not isinstance(message, dict):
                        raise RuntimeError('invalid plan guard receipt')
                    return message
            except (EOFError, OSError, ValueError) as exc:
                raise RuntimeError('plan guard worker disconnected') from exc
            if not self._process.is_alive():
                raise RuntimeError('plan guard worker exited: ' + str(self._process.exitcode))
        raise RuntimeError('plan guard worker closed')

    def __call__(self, observation, actions, epoch, request_id, *, committed_prefix=0,
                 max_new_waypoints=None):
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError('plan guard worker closed')
            self._sequence += 1
            identity = dict(sequence=self._sequence, epoch=epoch, request_id=request_id)
            request = dict(kind='check', **identity,
                state16=np.asarray(observation.state16).tolist(),
                full22=np.asarray(observation.full22).tolist(),
                actions=np.asarray(actions).tolist(), committed_prefix=committed_prefix,
                max_new_waypoints=max_new_waypoints)
            try:
                self._connection.send(request)
                response = self._receive(self.request_timeout_s)
                if response.get('kind') != 'checked' or any(response.get(k) != v for k, v in identity.items()):
                    raise RuntimeError('plan guard receipt identity mismatch')
                events = response.get('events')
                if not isinstance(events, list) or not events:
                    raise RuntimeError('plan guard receipt missing evidence')
                for event in events:
                    if (not isinstance(event, dict)
                            or event.get('event') not in ('policy_path_checked', 'policy_path_rejected')
                            or event.get('policy_epoch') != epoch or event.get('request_id') != request_id):
                        raise RuntimeError('invalid plan guard event identity')
                if response.get('ok') is True:
                    last = events[-1]
                    expected = len(actions)-committed_prefix
                    if max_new_waypoints is not None:
                        expected = min(expected, max_new_waypoints)
                    if (last['event'] != 'policy_path_checked'
                            or last.get('checked_samples') != expected*2
                            or last.get('screened_new_waypoints') != expected):
                        raise RuntimeError('plan guard receipt incomplete')
            except BaseException:
                # No implicit worker restart or unchecked action on IPC failure.
                self.close()
                raise
            if self._closed.is_set():
                raise RuntimeError('plan guard worker closed')
            for event in events:
                fields = dict(event)
                self.emit(fields.pop('event'), **fields)
            if response.get('ok') is not True:
                if response.get('candidate_rejected'):
                    from .robot_io import CandidateSafetyRejected
                    from .contract import ArmLimitRejected
                    error = ArmLimitRejected if response.get('rejection_type') == 'arm_limit' else CandidateSafetyRejected
                    raise error(response.get('error', 'plan candidate rejected'))
                self.close()
                raise RuntimeError('plan guard failed: ' + str(response.get('error')))

    def close(self):
        # Never wait for __call__'s lock: shutdown must interrupt an in-flight
        # check even when its native backend hangs. No ROS endpoints exist here.
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            process = self._process
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=.5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=.5)
            self._connection.close()


class RestartablePlanGuard:
    """Recreate a failed numeric worker only on a newly authorized epoch.

    Creation/IPC stays on the inference thread. A failed request is never
    retried automatically, and close cancels even a replacement's startup.
    """
    def __init__(self, spec, emit, **options):
        import threading
        self.spec, self.emit, self.options = spec, emit, options
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.failed_epoch = None
        self.worker = self._create()

    def _create(self):
        return ProcessPolicyPlanGuard(self.spec, self.emit,
            stop_requested=self.closed.is_set, **self.options)

    def __call__(self, observation, actions, epoch, request_id, **options):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError('plan guard supervisor closed')
            if self.worker._closed.is_set():
                if self.failed_epoch is None or epoch <= self.failed_epoch:
                    raise RuntimeError('plan guard requires a new control epoch')
                worker = self._create()
                self.worker = worker
                if self.closed.is_set():
                    worker.close()
                    raise RuntimeError('plan guard supervisor closed')
                self.emit('policy_guard_restarted', policy_epoch=epoch,
                          request_id=request_id, unchecked_action_approved=False)
            try:
                result = self.worker(observation, actions, epoch, request_id, **options)
            except Exception:
                if self.worker._closed.is_set():
                    self.failed_epoch = epoch
                raise
            if self.closed.is_set():
                raise RuntimeError('plan guard supervisor closed')
            return result

    def close(self):
        self.closed.set()
        self.worker.close()


def _guard_process_main(connection, spec, factory_path):
    """Spawn entry point. Imports only numeric protection, never robot IO nodes."""
    import importlib
    import signal
    from types import SimpleNamespace
    # The parent owns Ctrl+C and bounded shutdown; avoid sibling teardown races.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        module, name = factory_path.split(':', 1)
        gate = getattr(importlib.import_module(module), name)(spec)
        events = []
        def emit(event, **fields):
            events.append(dict(event=event, monotonic_ns=time.monotonic_ns(), **fields))
        guard = PolicyPlanGuard(gate, gate.mapping, emit)
        connection.send(dict(kind='ready'))
        while True:
            request = connection.recv()
            if not isinstance(request, dict) or request.get('kind') != 'check':
                raise RuntimeError('invalid plan guard request')
            events.clear()
            identity = {k: request[k] for k in ('sequence', 'epoch', 'request_id')}
            try:
                guard(SimpleNamespace(state16=request['state16'], full22=request['full22']),
                      np.asarray(request['actions']), request['epoch'], request['request_id'],
                      committed_prefix=request['committed_prefix'],
                      max_new_waypoints=request['max_new_waypoints'])
                response = dict(kind='checked', **identity, ok=True, events=events)
            except Exception as exc:
                from .robot_io import CandidateSafetyRejected
                from .contract import ArmLimitRejected
                response = dict(kind='checked', **identity, ok=False, events=events,
                    candidate_rejected=isinstance(exc, (CandidateSafetyRejected, ArmLimitRejected)),
                    rejection_type='arm_limit' if isinstance(exc, ArmLimitRejected) else 'collision',
                    error=type(exc).__name__ + ': ' + str(exc))
            connection.send(response)
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            connection.send(dict(kind='startup_failed', error=type(exc).__name__ + ': ' + str(exc)))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
