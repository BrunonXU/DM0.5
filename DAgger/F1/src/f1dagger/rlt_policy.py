"""Bound C16 RLT transport; the original B RTC endpoint is independent."""
import time
import threading
from urllib.request import Request

import numpy as np

from .policy import HttpPolicy
from .policy_errors import IKRejected
from .websocket_policy import WebSocketPolicy, encode_request

PROTOCOL = 'f1_eef_rlt_sync_v1'
RTC_PROTOCOL = 'f1_eef_rlt_rtc_v1'
IDENTITY_KEYS = ('base_model_version', 'encoder_manifest_sha256', 'actor_manifest_sha256',
                 'actor_version', 'checkpoint_step', 'training_phase', 'q_guidance_updates',
                 'execution_mode')


class RLTPolicy(WebSocketPolicy):
    websocket_path = '/infer'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rtc = self.config.get('rlt_rtc', False)
        if type(self.rtc) is not bool:
            raise ValueError('Explicit boolean RLT RTC contract required')
        self.local_segment_id = None
        self._pending_identity = None
        self._candidate_lock = threading.Lock()
        self._candidate_stop = threading.Event()
        self._candidate_thread = None
        self.candidate_error = None
        self.last_actor_sampling = None

    def _poll_candidates(self):
        while not self._candidate_stop.is_set():
            try:
                response = self.read(Request(self.endpoint+'/candidates'))
                candidate = response.get('candidate')
                if response.get('update_protocol') != 'local_boundary_v1':
                    raise ValueError('Actor update protocol mismatch')
                if candidate is not None:
                    identity = candidate['rlt']
                    for key in ('base_model_version', 'encoder_manifest_sha256', 'execution_mode'):
                        if identity[key] != self.config['expected_rlt'][key]:
                            raise ValueError('Actor update changed ' + key)
                    if any(key not in identity for key in IDENTITY_KEYS):
                        raise ValueError('Incomplete actor update')
                    with self._candidate_lock:
                        self._pending_identity = candidate
                self.candidate_error = response.get('error')
            except Exception as exc:
                self.candidate_error = str(exc)
            self._candidate_stop.wait(2.)

    def adopt_pending(self):
        """No network/disk wait. Called only on the next accepted → boundary."""
        with self._candidate_lock:
            candidate = self._pending_identity
            if candidate is None or self.inflight.locked():
                return False
            self.model_id, self.model_version = candidate['model_id'], candidate['model_version']
            self.rlt_identity = dict(candidate['rlt'])
            self._pending_identity = None
            return True

    def pin_evaluation(self, candidate):
        """Select a complete pre-registered identity before START, never latest."""
        if self.config.get('allow_boundary_updates') or self.local_segment_id is not None:
            raise ValueError('Evaluation identity must be pinned before collection')
        identity = candidate['rlt']
        if any(k not in identity for k in IDENTITY_KEYS):
            raise ValueError('Incomplete evaluation actor identity')
        if any(identity[k] != self.rlt_identity[k] for k in
               ('base_model_version','encoder_manifest_sha256','execution_mode')):
            raise ValueError('Evaluation actor changes frozen feature or execution contract')
        if not candidate.get('model_id') or not candidate.get('model_version'):
            raise ValueError('Evaluation model identity missing')
        self.model_id, self.model_version = candidate['model_id'], candidate['model_version']
        self.rlt_identity = dict(identity)

    def _start_candidate_poll(self):
        if self.config.get('allow_boundary_updates', False) and (
                self._candidate_thread is None or not self._candidate_thread.is_alive()):
            self._candidate_stop.clear()
            self._candidate_thread = threading.Thread(target=self._poll_candidates, daemon=True,
                                                      name='rlt-candidate-metadata')
            self._candidate_thread.start()

    def health(self):
        result = HttpPolicy.health(self)
        expected = dict(protocol=RTC_PROTOCOL if self.rtc else PROTOCOL, prediction_horizon=16, reference_horizon=50,
                        execute_steps=16, rtc=self.rtc, sync=not self.rtc, control_mode='rlt_hil_supervised')
        if (any(result.get(k) != v for k, v in expected.items())
                or self.config['chunk_steps'] != 16 or self.config['frequency_hz'] != 30
                or not self.config.get('model_version')
                or self.model_version != self.config['model_version']):
            raise ValueError('RLT C16 protocol/version mismatch')
        identity = result.get('rlt')
        if not isinstance(identity, dict) or any(k not in identity for k in IDENTITY_KEYS):
            raise ValueError('RLT component identity missing')
        if not self.config.get('expected_rlt') or identity != self.config['expected_rlt']:
            raise ValueError('RLT component identity mismatch')
        if identity['execution_mode'] != ('rtc_c16_committed_prefix_v1' if self.rtc else 'sync_c16_hold_then_execute'):
            raise ValueError('RLT execution contract mismatch')
        self.rlt_identity = dict(identity)
        self.pixel_encoder = None
        masked = self.config.get('rtc_masked_left_transport', False)
        if type(masked) is not bool or (masked and not self.config.get('rtc_pixels_directory')):
            raise ValueError('RLT masked transport requires bound preprocessing')
        if self.config.get('rtc_pixels_directory'):
            from .rtc_pixels import PixelEncoder
            encoder = PixelEncoder(self.config['rtc_pixels_directory'], result['input_preprocessing'],
                workers=self.config.get('rtc_pixel_workers', 1), masked_left_transport=masked)
            if masked and result.get('image_transport') != encoder.transport_identity:
                encoder.close()
                raise ValueError('RLT image transport identity mismatch')
            self.pixel_encoder = encoder
        self._start_candidate_poll()
        return result

    def close(self):
        self._candidate_stop.set()
        super().close()
        encoder = getattr(self, 'pixel_encoder', None)
        if encoder is not None:
            encoder.close()

    def infer_rtc(self, obs, request_id, epoch, context):
        return self.infer(obs, request_id, epoch, context=context)

    def infer(self, obs, request_id, epoch, *, context=None):
        if (context is not None) != self.rtc:
            raise ValueError('RLT request execution contract mismatch')
        self._start_candidate_poll()
        if self.model_id is None or not self.inflight.acquire(blocking=False):
            raise ValueError('RLT not bound or already in flight')
        started = time.perf_counter()
        result, packed, verified, outcome = {}, None, False, 'error'
        try:
            meta = dict(contract='f1_upper_16d_v1', protocol=RTC_PROTOCOL if self.rtc else PROTOCOL, session_id=self.session_id,
                local_segment_id=self.local_segment_id,
                request_id=request_id, observation_id=obs.observation_id, policy_epoch=epoch,
                observation_monotonic_ns=obs.capture_started_monotonic_ns,
                state16=list(map(float, obs.state16)), last_submitted16=self.held_getter().tolist(),
                state_timestamps_ns=list(obs.state_timestamps_ns), chunk_steps=16, execute_steps=16,
                prediction_horizon=16, reference_horizon=50, frequency_hz=30.,
                control_mode='rlt_hil_supervised',
                model_id=self.model_id, model_version=self.model_version,
                shadow_only=bool(self.config.get('shadow_only')))
            if context is not None:
                meta['rtc'] = context
            if getattr(obs, 'state_valid_mask', None) is not None:
                meta.update(state_valid_mask=list(obs.state_valid_mask),
                            absent_state_semantics='zero_storage_placeholder_not_measurement')
            frames = obs.frames
            if getattr(self, 'pixel_encoder', None) is not None:
                frames = self.pixel_encoder.encode(frames)
                meta['prepared_pixels'] = self.pixel_encoder.identity
                if self.pixel_encoder.masked_left_transport:
                    meta['image_transport'] = self.pixel_encoder.transport_identity
            payload = encode_request(meta, frames)
            packed = time.perf_counter()
            result = self._exchange(payload)
            self.validate_identity(result, meta, obs)
            for key in ('protocol', 'session_id', 'prediction_horizon', 'reference_horizon',
                        'execute_steps', 'shadow_only', 'control_mode'):
                if result.get(key) != meta[key]:
                    raise ValueError('RLT response identity mismatch: ' + key)
            if (result.get('rlt') != self.rlt_identity or result.get('rtc') is not self.rtc
                    or result.get('sync') is not (not self.rtc) or result.get('proposal_only') is not True
                    or result.get('image_transport') != meta.get('image_transport')
                    or result.get('active_action_indices') != [7,8,9,10,11,12,13,15]):
                raise ValueError('RLT response component/execution identity mismatch')
            sampling = self.rlt_identity.get('actor_sampling')
            actor_seed = result.get('actor_seed')
            if result.get('actor_sampling') != sampling:
                raise ValueError('RLT actor sampling identity mismatch')
            if sampling and sampling.get('mode') == 'fixed_gaussian':
                if type(actor_seed) is not int or not 0 <= actor_seed < 2**63:
                    raise ValueError('RLT exploration seed missing or invalid')
            elif actor_seed is not None:
                raise ValueError('Deterministic RLT returned an exploration seed')
            self.last_actor_sampling = dict(contract=sampling, seed=actor_seed,
                request_id=request_id, model_version=self.model_version)
            if result.get('success') is False and result.get('reason') == 'ik_rejected' and 'error' not in result:
                details = result.get('diagnostics')
                if (result.get('actions') is not None or not isinstance(details, list)
                        or not 1 <= len(details) <= 16
                        or any(d.get('success') is not True for d in details[:-1])
                        or details[-1].get('success') is not False or details[-1].get('joints') is not None):
                    raise ValueError('Invalid RLT IK rejection')
                verified, outcome = True, 'ik_rejected'
                raise IKRejected(result)
            if result.get('success') is not True:
                raise ValueError('RLT rejected: ' + str(result.get('error', result.get('reason'))))
            actions = self.validate(result, meta, obs)
            if not np.isfinite(actions).all():
                raise ValueError('Nonfinite RLT plan')
            if context is not None:
                expected = {k:context[k] for k in ('bootstrap','delay_steps','shift_steps','coverage_steps','plan_origin_ns','base_plan_id')}
                absolute = np.asarray(result.get('absolute_eef10'), float)
                committed = np.asarray(context['committed_actions16'])
                if (result.get('rtc_context') != expected or absolute.shape != (16,10)
                        or not np.isfinite(absolute).all()
                        or (len(committed) and not np.array_equal(actions[:len(committed)], committed))):
                    raise ValueError('RLT RTC changed its context or committed prefix')
            verified, outcome = True, 'success'
            return (actions, absolute) if context is not None else actions
        except IKRejected:
            raise
        except Exception:
            self.close()
            raise
        finally:
            self.last_timing = dict(request_id=request_id, policy_epoch=epoch, session_id=self.session_id,
                outcome=outcome, response_verified=verified,
                pack_ms=None if packed is None else (packed-started)*1000,
                client_total_ms=(time.perf_counter()-started)*1000,
                network_rtt_ms=result.get('client_network_rtt_ms'),
                server=result.get('timing_ms') if verified else None,
                observation_to_response_ms=(self.clock()-obs.capture_started_monotonic_ns)/1e6)
            self.inflight.release()
