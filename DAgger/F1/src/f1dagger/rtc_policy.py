"""Versioned RTC transport, reusing the bounded persistent network child."""
import time

import numpy as np

from .policy import HttpPolicy
from .policy_errors import IKRejected
from .websocket_policy import WebSocketPolicy, encode_request

PROTOCOL = 'f1_eef_rtc_v2'


class RTCPolicy(WebSocketPolicy):
    def health(self):
        masked_left_transport = self.config.get('rtc_masked_left_transport', False)
        if type(masked_left_transport) is not bool:
            raise ValueError('RTC masked left transport must be a boolean')
        if masked_left_transport and not self.config.get('rtc_pixels_directory'):
            raise ValueError('RTC masked left transport requires bound pixel preprocessing')
        result = HttpPolicy.health(self)
        if (result.get('protocol') != PROTOCOL or result.get('rtc') is not True
                or result.get('prediction_horizon') != 50 or result.get('execute_steps') != 20):
            raise ValueError('RTC protocol capabilities mismatch')
        previous = getattr(self, 'pixel_encoder', None)
        if previous is not None:
            previous.close()
        self.pixel_encoder = None
        if self.config.get('rtc_pixels_directory'):
            from .rtc_pixels import PixelEncoder
            try:
                encoder=PixelEncoder(self.config['rtc_pixels_directory'],result['input_preprocessing'],
                                     workers=self.config.get('rtc_pixel_workers', 1),
                                     masked_left_transport=masked_left_transport)
                if masked_left_transport and result.get('image_transport') != encoder.transport_identity:
                    encoder.close()
                    raise ValueError('RTC masked left transport capability mismatch')
            except Exception:
                self.model_id = self.model_version = None
                raise
            self.pixel_encoder = encoder
        result['client_image_transport'] = (self.pixel_encoder.transport_identity if self.pixel_encoder is not None
                                            else dict(mode='original_rgb_v1', constant_cameras=[]))
        return result

    def close(self):
        super().close()
        encoder = getattr(self, 'pixel_encoder', None)
        if encoder is not None:
            encoder.close()

    def infer_rtc(self, obs, request_id, epoch, context):
        if self.model_id is None or not self.inflight.acquire(blocking=False):
            raise ValueError('RTC not bound or already in flight')
        started = time.perf_counter()
        result, metadata, packed, verified, outcome = {}, {}, None, False, 'error'
        try:
            metadata = dict(contract='f1_upper_16d_v1', protocol=PROTOCOL, session_id=self.session_id,
                request_id=request_id, observation_id=obs.observation_id, policy_epoch=epoch,
                observation_monotonic_ns=obs.capture_started_monotonic_ns,
                state16=list(map(float, obs.state16)), last_submitted16=self.held_getter().tolist(),
                state_timestamps_ns=list(obs.state_timestamps_ns), chunk_steps=20, execute_steps=20,
                prediction_horizon=50, frequency_hz=30., model_id=self.model_id, model_version=self.model_version,
                shadow_only=bool(self.config.get('shadow_only')), rtc=context)
            if getattr(obs, 'state_valid_mask', None) is not None:
                metadata['state_valid_mask'] = list(obs.state_valid_mask)
                metadata['absent_state_semantics'] = 'zero_storage_placeholder_not_measurement'
            frames=obs.frames
            if self.pixel_encoder is not None:
                frames=self.pixel_encoder.encode(frames)
                metadata['prepared_pixels']=self.pixel_encoder.identity
                if self.pixel_encoder.masked_left_transport:
                    metadata['image_transport'] = self.pixel_encoder.transport_identity
            payload = encode_request(metadata, frames)
            packed = time.perf_counter()
            result = self._exchange(payload)
            self.validate_identity(result, metadata, obs)
            if result.get('image_transport') != metadata.get('image_transport'):
                raise ValueError('RTC image transport identity mismatch')
            for key in ('protocol', 'session_id', 'prediction_horizon', 'execute_steps', 'shadow_only'):
                if result.get(key) != metadata[key]:
                    raise ValueError('RTC identity mismatch: ' + key)
            expected = {key: context[key] for key in ('bootstrap', 'delay_steps', 'shift_steps',
                        'coverage_steps', 'plan_origin_ns', 'base_plan_id')}
            if (result.get('rtc_context') != expected or result.get('rtc') is not True
                    or result.get('active_action_indices') != [7,8,9,10,11,12,13,15]):
                raise ValueError('RTC context/mask mismatch')
            if result.get('success') is False and result.get('reason') == 'ik_rejected' and 'error' not in result:
                details = result.get('diagnostics')
                if (result.get('actions') is not None or not isinstance(details, list) or not details
                        or len(details) > context['coverage_steps']
                        or any(x.get('success') is not True for x in details[:-1])
                        or details[-1].get('success') is not False or details[-1].get('joints') is not None):
                    raise ValueError('Invalid RTC IK rejection')
                verified, outcome = True, 'ik_rejected'
                raise IKRejected(result)
            if result.get('success') is not True:
                raise ValueError('RTC rejected: ' + str(result.get('error', result.get('reason'))))
            actions = np.asarray(result.get('actions'), float)
            absolute = np.asarray(result.get('absolute_eef10'), float)
            if (actions.shape != (context['coverage_steps'], 16) or absolute.shape != (50, 10)
                    or not np.isfinite(actions).all() or not np.isfinite(absolute).all()):
                raise ValueError('RTC nonfinite/invalid plan')
            for action in actions:
                self.validate_action(action)
            committed = np.asarray(context['committed_actions16'])
            if len(committed) and not np.array_equal(actions[:len(committed)], committed):
                raise ValueError('RTC changed committed prefix')
            verified, outcome = True, 'success'
            return actions, absolute
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
                transport_stages=result.get('client_transport_stages'),
                image_transport=metadata.get('image_transport'),
                validation_completed_ns=time.monotonic_ns(),
                server=result.get('timing_ms') if verified else None,
                observation_to_response_ms=(self.clock()-obs.capture_started_monotonic_ns)/1e6)
            self.inflight.release()
