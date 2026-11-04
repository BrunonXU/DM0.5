"""One outstanding numerical request; no ROS or output authority in the worker."""
import importlib
import multiprocessing
import signal
import time
from types import SimpleNamespace

import numpy as np

from .faults import ManualReferenceChanged


def intent_key(packet):
    # Magnitude varies continuously while a stick is held. Requiring exact
    # float equality would starve a pipelined controller. A reversal or release
    # invalidates the old work; bounded-age magnitudes retain their provenance.
    signs = lambda values: tuple(int(np.sign(x)) for x in values)
    return (packet.session_id, packet.active_side, packet.mode, signs(packet.axes),
            signs(packet.angular_axes), packet.speed_scale)


class ManualPlanner:
    """Nonblocking during control. Timeout uses the existing target lifetime.

    The child only receives snapshots and returns proposals. Release invalidates
    pending work without waiting for it. No queue of past joystick packets exists.
    """
    def __init__(self, spec, *, max_age_s, factory='f1dagger.manual_compute:NumericalPlanner',
                 startup_timeout=45., clock=time.monotonic_ns):
        self.clock = clock
        self.max_age_ns = int(max_age_s * 1e9)
        if self.max_age_ns <= 0:
            raise ValueError('manual target lifetime must be positive')
        self.generation = self.sequence = 0
        self.pending = None
        self.accepted = None
        self.last_request_ns = None
        self.closed = False
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child, spec, factory),
                                       name='f1-manual-compute', daemon=True)
        self.process.start()
        child.close()
        try:
            if not self.connection.poll(startup_timeout):
                raise RuntimeError('manual worker initialization timeout')
            ready = self.connection.recv()
            if ready.get('kind') != 'ready':
                raise RuntimeError('manual worker initialization failed: '+str(ready))
        except BaseException:
            self.close()
            raise

    def invalidate(self):
        self.generation += 1
        self.accepted = self.last_request_ns = None

    def poll(self, packet, full, held, dt, epoch):
        now = self.clock()
        if self.closed or not self.process.is_alive():
            raise ManualReferenceChanged('manual compute worker unavailable; release RB')
        key = intent_key(packet)
        if self.accepted is not None and self.accepted['key'] != key:
            self.invalidate()
        if (self.accepted is not None and
                np.max(np.abs(np.asarray(full)[14:]-self.accepted['reference'])) > .5):
            self.invalidate()
            raise ManualReferenceChanged('body/head reference changed during manual compute')
        result = None
        if self.pending is not None:
            pending = self.pending
            if self.connection.poll(0):
                try:
                    response = self.connection.recv()
                except (EOFError, OSError) as exc:
                    raise ManualReferenceChanged('manual compute disconnected') from exc
                self.pending = None
                if response.get('sequence') != pending['sequence']:
                    raise ManualReferenceChanged('manual compute receipt identity mismatch')
                valid = (pending['generation'] == self.generation and pending['epoch'] == epoch
                         and pending['key'] == key and 0 <= now-pending['sent'] <= self.max_age_ns)
                if valid:
                    if np.max(np.abs(np.asarray(full)[14:]-pending['reference'])) > .5:
                        self.invalidate()
                        raise ManualReferenceChanged('body/head reference changed during manual compute')
                    if response.get('ok') is not True:
                        self.invalidate()
                        kind = response.get('error_type')
                        if kind == 'ArmBoundaryHold':
                            from .teleop import ArmBoundaryHold
                            raise ArmBoundaryHold(response['error'], response.get('details'))
                        if kind == 'CandidateSafetyRejected':
                            from .robot_io import CandidateSafetyRejected
                            raise CandidateSafetyRejected(response['error'], details=response.get('details'))
                        raise ManualReferenceChanged('manual compute: '+response.get('error', 'failed'))
                    result = response
                    self.accepted = dict(key=key, sent=pending['sent'], response=response,
                                         reference=pending['reference'])
                else:
                    self.invalidate()  # Discard virtual lead computed for old intent.
            elif now-pending['sent'] > self.max_age_ns:
                self.invalidate()
                raise ManualReferenceChanged('manual compute exceeded target lifetime; release RB')
        if self.pending is None:
            self.sequence += 1
            step = dt if self.last_request_ns is None else (now-self.last_request_ns)/1e9
            if not np.isfinite(dt) or not 0 < dt <= .05 or step <= 0:
                self.invalidate()
                raise ManualReferenceChanged('invalid manual integration timestep')
            skipped_wait = step > .05
            if skipped_wait:
                # Worker scheduling delay is not a joystick integration period.
                # Drop its virtual reference, restart from current feedback and
                # integrate only this control tick. Never catch up lost time.
                self.invalidate()
                result = None
                step = dt
            self.pending = dict(sequence=self.sequence, generation=self.generation,
                                epoch=epoch, key=key, sent=now, reference=np.asarray(full)[14:].tolist())
            request = dict(self.pending, dt=step, full=np.asarray(full).tolist(),
                           held=np.asarray(held).tolist(), axes=tuple(packet.axes),
                           angular_axes=tuple(packet.angular_axes),
                           skipped_wait=skipped_wait,
                           packet_sequence=getattr(packet, 'sequence', None))
            # One bounded small message, only after the previous response was
            # consumed. The child never receives unbounded queued input history.
            try:
                self.connection.send(request)
            except (OSError, EOFError) as exc:
                self.pending = None
                self.invalidate()
                raise ManualReferenceChanged('manual compute disconnected during request') from exc
            self.last_request_ns = now
        return result

    def continuing(self, packet):
        return (self.accepted is not None and self.accepted['key'] == intent_key(packet)
                and 0 <= self.clock()-self.accepted['sent'] <= self.max_age_ns)

    def close(self):
        self.closed = True
        if self.process.pid is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(.5)
            if self.process.is_alive():
                self.process.kill(); self.process.join(.5)
        self.connection.close()


class NumericalPlanner:
    def __init__(self, spec):
        from .robot_io import collision_gate_from_spec
        from .teleop import F1ArmTarget
        self.gate = collision_gate_from_spec(spec['gate'])
        self.mapping = self.gate.mapping
        self.arm = F1ArmTarget(spec['urdf'], self.mapping, spec['ik'])
        self.arm.warmup(spec['gate']['full22'], np.asarray(spec['state']))
        self.generation = None

    def __call__(self, request):
        if request['generation'] != self.generation:
            self.arm.release()
            self.generation = request['generation']
        _, side, mode, _, _, speed = request['key']
        packet = SimpleNamespace(active_side=side, mode=mode, axes=request['axes'],
                                 angular_axes=request['angular_axes'], speed_scale=speed)
        full, held = np.asarray(request['full']), np.asarray(request['held'])
        candidate = self.arm.target(packet, full, held, request['dt'])
        selected, evidence = candidate, None
        from .robot_io import CandidateSafetyRejected
        try:
            self.gate.check_preventive(candidate, full)
        except CandidateSafetyRejected as exc:
            from .manual_candidate import shorten_manual_candidate
            physical = held.copy(); physical[:14] = full[:14]
            self.mapping.check_action(physical)
            self.gate.check_preventive(held, full)
            self.gate.check_preventive(physical, full)
            selected, evidence = shorten_manual_candidate(self.gate, self.mapping, candidate,
                                                         held, full, side, exc)
            self.arm.accept_safe_candidate(full, selected)
        return dict(candidate=candidate.tolist(), selected=selected.tolist(), shortened=evidence,
                    seed=self.arm.seed_diagnostics)


def _worker(connection, spec, factory):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        module, name = factory.split(':', 1)
        planner = getattr(importlib.import_module(module), name)(spec)
        connection.send(dict(kind='ready'))
        while True:
            request = connection.recv()
            started = time.monotonic_ns()
            try:
                result = dict(ok=True, **planner(request))
            except Exception as exc:
                result = dict(ok=False, error_type=type(exc).__name__, error=str(exc),
                              details=getattr(exc, 'details', {}))
            connection.send(dict(result, sequence=request['sequence'],
                                 packet_sequence=request['packet_sequence'],
                                 input_axes=request['axes'], input_angular_axes=request['angular_axes'],
                                 requested_monotonic_ns=request['sent'],
                                 compute_ms=(time.monotonic_ns()-started)/1e6))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            connection.send(dict(kind='failed', error=str(exc)))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
