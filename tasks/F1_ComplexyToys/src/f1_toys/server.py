"""Bounded synchronous binary WebSocket inference. No robot/control dependency."""
import argparse
import asyncio
from io import BytesIO
import json
from pathlib import Path
import struct
import time
import hashlib
import threading
import traceback

from aiohttp import web, WSMsgType
import numpy as np
from PIL import Image

from .inference import Policy
from .input_optimization import configure_image_transport, environment_switch
from .rtc_prefix_optimization import prefix_graph_status
from .rlt_contract import RLT_PROTOCOL, RLT_CONTROL_MODE, RLT_RTC_PROTOCOL

CONTRACT = "f1_upper_16d_v1"
PROTOCOL = "f1_eef_sync_v2"
RTC_PROTOCOL = "f1_eef_rtc_v2"
CAMERAS = ["head", "left_wrist", "right_wrist"]
NAMES = [f"arm_{side}_j{i}" for side in ("l", "r") for i in range(1, 8)] + ["left_gripper_position", "right_gripper_position"]


def unpack(payload, *, rtc_enabled=False, preprocessing=None, image_transport=None, rlt_enabled=False, rlt_rtc_enabled=False):
    if not isinstance(payload, bytes) or not 4 < len(payload) <= 12 * 1024**2:
        raise ValueError("Expected bounded binary payload")
    length, = struct.unpack("!I", payload[:4])
    if not 1 <= length <= 65536 or 4 + length > len(payload):
        raise ValueError("Invalid metadata size")
    meta = json.loads(payload[4:4+length])
    if rtc_enabled and rlt_enabled:
        raise ValueError("RLT C16 does not support RTC")
    expected_protocol = (RLT_RTC_PROTOCOL if rlt_rtc_enabled else RLT_PROTOCOL) if rlt_enabled else RTC_PROTOCOL if rtc_enabled else PROTOCOL
    if not isinstance(meta, dict) or meta.get("protocol") != expected_protocol or meta.get("contract") != CONTRACT:
        raise ValueError("Protocol/contract mismatch")
    if not (rtc_enabled or rlt_rtc_enabled) and any(k in meta for k in ("rtc", "prefix", "committed_prefix", "delay_steps", "prefix_actions")):
        raise ValueError("RTC not supported")
    if rtc_enabled:
        validate_rtc(meta.get('rtc'), meta.get('request_id'))
    if rlt_rtc_enabled:
        if not rlt_enabled:
            raise ValueError('RLT RTC requires its dedicated endpoint')
        validate_rtc(meta.get('rtc'), meta.get('request_id'), horizon=16)
    prepared=meta.get('prepared_pixels')
    if prepared is not None and (not (rtc_enabled or rlt_enabled) or prepared != preprocessing):
        raise ValueError('Prepared pixel identity mismatch')
    if image_transport is None:
        if 'image_transport' in meta:
            raise ValueError('Masked image transport is not enabled on this service')
        transport_receipt = None
    else:
        transport_receipt = image_transport.validate_metadata(meta)
    if rlt_enabled:
        if (meta.get("prediction_horizon") != 16 or meta.get("reference_horizon") != 50
                or meta.get("execute_steps") != 16 or meta.get("chunk_steps") != 16
                or meta.get("frequency_hz") != 30 or meta.get("control_mode") != RLT_CONTROL_MODE):
            raise ValueError("Supervised sync C16 / reference H50 / 30Hz required")
    elif meta.get("prediction_horizon") != 50 or meta.get("execute_steps") != 20 or meta.get("chunk_steps") != 20 or meta.get("frequency_hz") != 30:
        raise ValueError("H50 prediction / H20 execution / 30Hz required")
    for k in ("request_id", "observation_id", "policy_epoch", "observation_monotonic_ns"):
        if type(meta.get(k)) is not int or meta[k] < 0:
            raise ValueError("Invalid identity: " + k)
    if not isinstance(meta.get("session_id"), str) or not 1 <= len(meta["session_id"]) <= 128:
        raise ValueError("Missing session identity")
    for k in ("state16", "last_submitted16"):
        a = np.asarray(meta.get(k), float)
        if a.shape != (16,) or not np.isfinite(a).all():
            raise ValueError("Invalid " + k)
    entries = meta.get("frames", [])
    if [x.get("camera") for x in entries] != CAMERAS:
        raise ValueError("Ordered three cameras required")
    offset = 4 + length
    images = {}
    for entry in entries:
        n = entry.get("length")
        if type(n) is not int or not 1 <= n <= 4*1024**2 or offset+n > len(payload):
            raise ValueError("Invalid image length")
        if entry.get("encoding") not in ("image/jpeg", "image/png"):
            raise ValueError("Unsupported image encoding")
        if transport_receipt is not None:
            image_transport.validate_frame(entry, payload[offset:offset+n])
        with Image.open(BytesIO(payload[offset:offset+n])) as image:
            expected = (224,224) if prepared is not None else ((1280, 720) if entry["camera"] == "head" else (640, 480))
            if prepared is not None and entry['encoding'] != 'image/png':
                raise ValueError('Prepared pixels require lossless PNG')
            if image.size != expected or [image.width, image.height] != [entry.get("width"), entry.get("height")]:
                raise ValueError("Camera resolution changed")
            images[entry["camera"]] = image.convert("RGB")
        offset += n
    if offset != len(payload):
        raise ValueError("Unexpected payload suffix")
    return meta, images


def validate_rtc(rtc, request_id, *, horizon=50):
    if not isinstance(rtc, dict) or type(rtc.get('bootstrap')) is not bool:
        raise ValueError('RTC context required')
    for key in ('delay_steps', 'shift_steps', 'coverage_steps', 'plan_origin_ns', 'base_plan_id'):
        if type(rtc.get(key)) is not int or rtc[key] < 0:
            raise ValueError('Invalid RTC ' + key)
    d, s, k = (rtc[key] for key in ('delay_steps', 'shift_steps', 'coverage_steps'))
    covered = s+d+1 <= k <= 50 if horizon == 50 else k == 16 and s+d <= k
    if not (1 <= d <= s <= horizon-d and covered):
        raise ValueError('RTC timing/coverage invalid')
    if rtc['base_plan_id'] >= request_id:
        raise ValueError('RTC base plan must precede request')
    for key, shape in (('committed_actions16', (0 if rtc['bootstrap'] else d, 16)),
                       ('previous_absolute_eef10', (0 if rtc['bootstrap'] else horizon-s, 10))):
        value = np.asarray(rtc.get(key), float)
        if shape[0] == 0:
            valid = value.shape == (0,)
        else:
            valid = value.shape == shape
        if not valid or not np.isfinite(value).all():
            raise ValueError('Invalid RTC ' + key)
    if rtc['bootstrap'] != (rtc['base_plan_id'] == 0):
        raise ValueError('RTC bootstrap/base plan mismatch')


class Server:
    def __init__(self, policy, *, steps=10, log=None, rtc=False, state_input_profile=None,
                 image_transport=None, inference_worker=None, rlt_policy=None):
        self.policy, self.steps, self.log = policy, steps, log
        self.rtc = rtc
        self.state_input_profile = state_input_profile
        self.image_transport = image_transport
        self.inference_worker = inference_worker
        self.rlt_policy = rlt_policy
        self.busy = False
        self.log_lock = threading.Lock()
        self.identity = "f1-toys-eef-" + policy.bundle_id[:16]

    async def health(self, request):
        prefix = prefix_graph_status(self.policy)
        health = dict(contract=CONTRACT, ready=not prefix['enabled'] or prefix['graph_ready'], model_id=self.identity,
            model_version=self.policy.bundle_id, protocol=RTC_PROTOCOL if self.rtc else PROTOCOL, sync=not self.rtc, rtc=self.rtc,
            prediction_horizon=50, execute_steps=20, denoise_steps=self.steps,
            rtc_optimization=getattr(self.policy, 'rtc_optimization', 'eager'),
            rtc_prefix_graph=prefix,
            rtc_prefix_graph_diagnostics=getattr(getattr(self.policy, 'rtc_prefix_graph', None), 'diagnostics', {}),
            input_preprocessing=getattr(self.policy, 'input_preprocessing', None),
            capabilities=dict(cameras=CAMERAS, state_names=NAMES, action_names=NAMES,
                units=["degree"]*14+["0..100"]*2, action_type="absolute", frequency_hz=30., chunk_steps=20))
        if self.state_input_profile is not None:
            health['state_input_profile'] = self.state_input_profile.identity
        if self.image_transport is not None:
            health['image_transport'] = self.image_transport.identity
        return web.json_response(health)

    async def rlt_health(self, request):
        if self.rlt_policy is None:
            raise web.HTTPNotFound(text="RLT candidate not configured")
        prefix = prefix_graph_status(self.policy)
        rlt_rtc = bool(getattr(self.rlt_policy, 'rtc', False))
        health = dict(contract=CONTRACT, protocol=RLT_RTC_PROTOCOL if rlt_rtc else RLT_PROTOCOL,
            ready=bool(self.rlt_policy.ready and (not prefix['enabled'] or prefix['graph_ready'])),
            **getattr(self.rlt_policy, "initial_identity", self.rlt_policy.identity), sync=not rlt_rtc, rtc=rlt_rtc,
            prediction_horizon=16, reference_horizon=50, execute_steps=16, denoise_steps=5,
            reference_compute_path=getattr(self.rlt_policy,'reference_compute_path','training_eager_prefix_and_suffix'),
            proposal_only=True, control_mode=RLT_CONTROL_MODE,
            input_preprocessing=getattr(self.policy, 'input_preprocessing', None),
            capabilities=dict(cameras=CAMERAS, state_names=NAMES, action_names=NAMES,
                units=["degree"]*14+["0..100"]*2, action_type="absolute", frequency_hz=30., chunk_steps=16))
        if 'runtime_manifest' in health:
            health['runtime_manifest']=dict(health['runtime_manifest'],reference_compute_path=health['reference_compute_path'])
        if self.image_transport is not None:
            health['image_transport'] = self.image_transport.identity
        return web.json_response(health)

    async def rlt_candidate(self, request):
        registry = getattr(self.rlt_policy, 'snapshots', None)
        return web.json_response(dict(contract=CONTRACT,
            update_protocol='local_boundary_v1',
            candidate=None if registry is None else registry.candidate(),
            error=None if registry is None else registry.error))

    def compute_rlt_features(self, payload, echo=False, received_ns=None):
        meta, images = unpack(payload, rlt_enabled=True,
            preprocessing=getattr(self.policy, 'input_preprocessing', None), image_transport=self.image_transport)
        identity = getattr(self.rlt_policy, 'feature_identity', self.rlt_policy.initial_identity)
        if meta.get('model_version') != identity['model_version']:
            raise ValueError('Feature extraction base/encoder identity mismatch')
        features = self.rlt_policy.predict(images, meta['state16'], meta['last_submitted16'],
            prepared_pixels=meta.get('prepared_pixels') is not None, seed=meta['observation_id'], features_only=True)
        return dict(contract=CONTRACT, success=True, features=features,
                    reference_compute_path='training_eager_prefix_and_suffix',
                    observation_id=meta['observation_id'], model_version=identity['model_version'],
                    base_model_version=identity['rlt']['base_model_version'],
                    encoder_manifest_sha256=identity['rlt']['encoder_manifest_sha256'],
                    feature_only=True, executed_transition=False)

    def compute_rlt(self, payload, echo=False, received_ns=None):
        if self.rlt_policy is None or not self.rlt_policy.ready or echo:
            raise ValueError("RLT candidate is not ready for supervised inference")
        started, compute_ns, received_wall_ns = time.perf_counter(), time.monotonic_ns(), time.time_ns()
        rlt_rtc = bool(getattr(self.rlt_policy, 'rtc', False))
        meta, images = unpack(payload, rlt_enabled=True, rlt_rtc_enabled=rlt_rtc,
            preprocessing=getattr(self.policy, 'input_preprocessing', None), image_transport=self.image_transport)
        if hasattr(self.rlt_policy, 'select_snapshot'):
            self.rlt_policy.select_snapshot(meta.get('model_version'), meta.get('local_segment_id'))
        identity = self.rlt_policy.identity
        if meta.get('model_id') != identity['model_id'] or meta.get('model_version') != identity['model_version']:
            raise ValueError("RLT composite model binding mismatch")
        decoded_at = time.perf_counter()
        extra = {'rtc': meta['rtc']} if rlt_rtc else {}
        result, raw, replay = self.rlt_policy.predict(images, meta['state16'], meta['last_submitted16'],
            prepared_pixels=meta.get('prepared_pixels') is not None, **extra)
        result.update({key: meta[key] for key in ('contract', 'protocol', 'session_id', 'request_id',
            'observation_id', 'policy_epoch', 'observation_monotonic_ns', 'frequency_hz')})
        result.update(model_id=identity['model_id'], model_version=identity['model_version'], rlt=identity['rlt'],
            reference_compute_path=replay.get('reference_compute_path','training_eager_prefix_and_suffix'),
            actor_sampling=replay.get('actor_sampling'), actor_seed=replay.get('actor_seed'),
            local_segment_id=meta.get('local_segment_id'),
            prediction_horizon=16, reference_horizon=50, execute_steps=16, chunk_steps=16,
            active_action_indices=[7,8,9,10,11,12,13,15], rtc=rlt_rtc, sync=not rlt_rtc, proposal_only=True,
            control_mode=RLT_CONTROL_MODE, shadow_only=bool(meta.get('shadow_only', False)))
        if rlt_rtc:
            result['rtc_context'] = {k:meta['rtc'][k] for k in ('bootstrap','delay_steps','shift_steps','coverage_steps','plan_origin_ns','base_plan_id')}
        if 'image_transport' in meta:
            result['image_transport'] = dict(self.image_transport.identity)
        result['timing_ms'].update(decode=(decoded_at-started)*1000, handler=(time.perf_counter()-started)*1000)
        log_started = time.perf_counter()
        if self.log:
            record = dict(result, telemetry_schema='f1_rlt_policy_proposal_v1',
                received_wall_ns=received_wall_ns, completed_wall_ns=time.time_ns(),
                payload_sha256=hashlib.sha256(payload).hexdigest(), executed_transition=False,
                prediction_replay=dict(replay, state16=meta['state16'], last_submitted16=meta['last_submitted16'],
                    normalized_actions32=np.asarray(raw).tolist(), frames=meta['frames'],
                    state_timestamps_ns=meta.get('state_timestamps_ns'),
                    state_valid_mask=meta.get('state_valid_mask'), model_version=identity['model_version']))
            if result.get('success') is False:
                record['failure_replay'] = record['prediction_replay']
            self.write_record(record)
        result['timing_ms'].update(log_and_replay=(time.perf_counter()-log_started)*1000,
            worker_total=(time.perf_counter()-started)*1000,
            worker_queue=0. if received_ns is None else (compute_ns-received_ns)/1e6)
        return result

    def compute(self, payload, echo, received_ns=None):
        started = time.perf_counter()
        compute_ns = time.monotonic_ns()
        received_wall_ns = time.time_ns()
        meta, images = unpack(payload, rtc_enabled=self.rtc,
                              preprocessing=getattr(self.policy,'input_preprocessing',None),
                              image_transport=self.image_transport)
        if meta.get("model_id") != self.identity or meta.get("model_version") != self.policy.bundle_id:
            raise ValueError("Model binding mismatch")
        profile_receipt = (self.state_input_profile.validate_request(meta)
                           if self.state_input_profile is not None else None)
        decoded_at = time.perf_counter()
        if echo:
            result = dict(success=True, actions=None, echo=True, timing_ms={})
        elif self.rtc:
            result, raw, _ = self.policy.predict_rtc(images, meta['state16'], meta['last_submitted16'], meta['rtc'], steps=self.steps,
                                                   prepared_pixels=meta.get('prepared_pixels') is not None)
        else:
            result, raw, _ = self.policy.predict_eef(images, meta["state16"], meta["last_submitted16"], steps=self.steps)
        result.update({k: meta[k] for k in ("contract", "protocol", "session_id", "request_id", "observation_id",
                                          "policy_epoch", "observation_monotonic_ns", "frequency_hz")})
        result.update(model_id=self.identity, model_version=self.policy.bundle_id,
                      prediction_horizon=50, execute_steps=20, active_action_indices=[7,8,9,10,11,12,13,15], rtc=self.rtc)
        if profile_receipt is not None:
            result['state_input_profile'] = profile_receipt
        if 'image_transport' in meta:
            result['image_transport'] = dict(self.image_transport.identity)
        if self.rtc:
            result['rtc_context'] = {key: meta['rtc'][key] for key in
                ('bootstrap', 'delay_steps', 'shift_steps', 'coverage_steps', 'plan_origin_ns', 'base_plan_id')}
        result["shadow_only"] = bool(meta.get("shadow_only", False))
        result["timing_ms"].update(decode=(decoded_at-started)*1000, handler=(time.perf_counter()-started)*1000)
        log_started = time.perf_counter()
        if self.log:
            record = dict(result)
            if self.rtc:
                record['rtc_request'] = meta['rtc']
            record.update(telemetry_schema='f1_policy_telemetry_v1',
                          received_wall_ns=received_wall_ns, completed_wall_ns=time.time_ns(),
                          payload_sha256=hashlib.sha256(payload).hexdigest())
            if not echo:
                # All requests, including successes; never add raw tensors to the wire response.
                record['prediction_replay'] = dict(state16=meta['state16'],
                    last_submitted16=meta['last_submitted16'],
                    normalized_actions32=np.asarray(raw).tolist(), denoise_steps=self.steps,
                    state_timestamps_ns=meta.get('state_timestamps_ns'), frames=meta['frames'],
                    reconstruction='physical_actions -> fixed raw-state FK anchor -> relative_pose',
                    model_version=self.policy.bundle_id)
                if profile_receipt is not None:
                    record['prediction_replay'].update(
                        state_valid_mask=meta['state_valid_mask'],
                        absent_state_semantics=meta['absent_state_semantics'],
                        state_input_profile=profile_receipt)
                if result.get('success') is False:
                    record['failure_replay'] = record['prediction_replay']
                from .native.kinematics import LOW_DEG, HIGH_DEG
                margins = []
                for step, diag in enumerate(result.get('diagnostics', [])):
                    q = np.asarray(diag.get('candidate_joints'), dtype=float)
                    if q.shape == (7,) and np.isfinite(q).all():
                        margins.append(dict(step=step, lower_deg=(q-LOW_DEG).tolist(),
                            upper_deg=(HIGH_DEG-q).tolist(),
                            wrist_coupling_deg=float(110.5-1.025*abs(q[5])-abs(q[6]))))
                record['ik_limit_margins'] = margins
            self.write_record(record)
        result['timing_ms'].update(log_and_replay=(time.perf_counter()-log_started)*1000,
            worker_total=(time.perf_counter()-started)*1000,
            worker_queue=0. if received_ns is None else (compute_ns-received_ns)/1e6)
        return result

    def write_record(self, record):
        if self.log:
            with self.log_lock, self.log.open('a') as stream:
                stream.write(json.dumps(record, allow_nan=False) + '\n')

    def record_error(self, payload, reason, trace=None, *, rlt=False):
        record = dict(telemetry_schema='f1_policy_telemetry_v1',
            event='request_error', error=reason, traceback=trace,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            logged_wall_ns=time.time_ns(), model_id=self.identity,
            model_version=self.policy.bundle_id, response_verified=False)
        if rlt and self.rlt_policy is not None:
            record.update(model_id=self.rlt_policy.identity['model_id'],
                          model_version=self.rlt_policy.identity['model_version'],
                          rlt=self.rlt_policy.identity['rlt'], executed_transition=False)
        if self.state_input_profile is not None:
            record['state_input_profile'] = self.state_input_profile.identity
        self.write_record(record)

    async def websocket(self, request):
        rlt = request.path in ('/rlt/infer', '/rlt/features')
        if rlt and self.rlt_policy is None:
            raise web.HTTPNotFound(text="RLT candidate not configured")
        socket = web.WebSocketResponse(compress=False, max_msg_size=12*1024**2, heartbeat=30)
        await socket.prepare(request)
        async for msg in socket:
            received_ns = time.monotonic_ns()
            if msg.type != WSMsgType.BINARY:
                await socket.close(code=1003, message=b"binary required")
                break
            feature_request = request.path == '/rlt/features'
            if not feature_request:
                self.last_control_request_ns = received_ns
                # Background extraction only starts during a quiet interval.
                # A new physical episode may start meanwhile: wait for that one
                # feature forward instead of rejecting the robot as "busy".
                while getattr(self, 'busy_kind', None) == 'features':
                    await asyncio.sleep(.005)
            if self.busy or (feature_request and received_ns-getattr(self, 'last_control_request_ns', 0) < 2_000_000_000):
                self.record_error(msg.data, 'busy', rlt=rlt)
                await socket.send_bytes(json.dumps(dict(success=False, error="busy")).encode())
                continue
            self.busy = True
            self.busy_kind = 'features' if feature_request else 'control'
            try:
                compute = self.compute_rlt_features if request.path == '/rlt/features' else self.compute_rlt if rlt else self.compute
                if self.inference_worker is None:
                    result = await asyncio.to_thread(compute, msg.data, request.path == "/echo", received_ns)
                else:
                    result = await self.inference_worker.run(compute, msg.data, request.path == "/echo", received_ns)
            except Exception as exc:
                result = dict(success=False, error=f"{type(exc).__name__}: {exc}")
                self.record_error(msg.data, result['error'], traceback.format_exc(), rlt=rlt)
            finally:
                self.busy = False
                self.busy_kind = None
            worker_returned_ns = time.monotonic_ns()
            encoded = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
            encoded_ns = time.monotonic_ns()
            await socket.send_bytes(encoded)
            sent_ns = time.monotonic_ns()
            if self.log:
                transport = dict(event='server_transport', session_id=result.get('session_id'),
                    request_id=result.get('request_id'), received_ns=received_ns,
                    worker_returned_ns=worker_returned_ns, encoded_ns=encoded_ns, sent_ns=sent_ns,
                    response_bytes=len(encoded), timing_ms=result.get('timing_ms'))
                await asyncio.to_thread(self.write_transport, transport)
        return socket

    def write_transport(self, record):
        # Separate stream: existing raw prediction replay format stays intact.
        with self.log_lock, self.log.with_suffix('.transport.jsonl').open('a') as stream:
            stream.write(json.dumps(record, allow_nan=False)+'\n')

    async def close(self, app=None):
        if self.inference_worker is not None:
            await asyncio.to_thread(self.inference_worker.close)
        if self.rlt_policy is not None and hasattr(self.rlt_policy, 'close'):
            await asyncio.to_thread(self.rlt_policy.close)


def warm_policy(policy, dataset, held, *, steps, rtc, graph_transitions=False):
    """Run all graph modes on the same worker that will serve requests."""
    for _ in range(2):
        policy.predict_eef(dataset.raw_images(0), dataset.states[0], held, steps=steps)
    if rtc:
        bootstrap = dict(bootstrap=True, delay_steps=15, shift_steps=20, coverage_steps=37,
                         plan_origin_ns=0, base_plan_id=0, committed_actions16=[], previous_absolute_eef10=[])
        result, _, _ = policy.predict_rtc(dataset.raw_images(0), dataset.states[0], held, bootstrap, steps=steps)
        # Warm the complete VJP even when this observation's IK rejects.
        guided = dict(bootstrap, bootstrap=False, base_plan_id=1,
                      previous_absolute_eef10=result['absolute_eef10'][20:],
                      committed_actions16=[held.tolist()]*15)
        policy.predict_rtc(dataset.raw_images(0), dataset.states[0], held, guided, steps=steps)
        if graph_transitions:
            # Exercise the no-grad -> full-VJP -> no-grad -> full-VJP lifecycle.
            policy.predict_rtc(dataset.raw_images(0), dataset.states[0], held, bootstrap, steps=steps)
            policy.predict_rtc(dataset.raw_images(0), dataset.states[0], held, guided, steps=steps)
            if not prefix_graph_status(policy)['graph_ready']:
                raise RuntimeError('Prefix graph warmup completed without a ready graph')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", required=True)
    p.add_argument("--rlinf-root", required=True)
    p.add_argument("--dataset", required=True, help="One real observation for pre-ready warmup")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8096)
    p.add_argument("--steps", type=int, choices=[5,10], default=10)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument('--rtc', action='store_true', help='Official inference-time VJP RTC, dedicated protocol')
    p.add_argument('--rtc-compile-suffix', action='store_true', help='Validated action-expert compilation, including VJP')
    p.add_argument('--state-input-profile', choices=['legacy_left_gripper_zero_v1'],
                   help='Explicit model-only completion for a proven legacy training constant')
    p.add_argument('--rlt-snapshots', type=Path, help='Validated local actor registry; activation only at segment boundaries')
    p.add_argument('--rlt-rtc', action='store_true', help='Versioned C16 RTC with an immutable committed prefix')
    p.add_argument('--rlt-head', type=Path, help='Immutable BC export for supervised C16 sidecar')
    p.add_argument('--rlt-encoder', type=Path, help='Frozen encoder bound to the same VLA and BC export')
    p.add_argument('--rlt-rlinf-root', type=Path, help='Frozen RLT MLP/token core; does not replace the VLA core')
    from .rlt_inference import REFERENCE_COMPUTE_PATHS
    p.add_argument('--rlt-reference-compute',choices=REFERENCE_COMPUTE_PATHS,
                   default=REFERENCE_COMPUTE_PATHS[0],help='Versioned RLT compute path; does not enable continuous execution')
    a = p.parse_args()
    if (a.rlt_head is None) != (a.rlt_encoder is None):
        p.error('--rlt-head and --rlt-encoder must be supplied together')
    if a.rlt_head is None and a.rlt_reference_compute!=REFERENCE_COMPUTE_PATHS[0]:
        p.error('--rlt-reference-compute requires an RLT sidecar')
    if a.rlt_rtc and a.rlt_head is None:
        p.error('--rlt-rtc requires an RLT sidecar')
    if a.rlt_head is not None and (not a.rtc or a.steps != 5 or a.state_input_profile is not None):
        p.error('RLT sidecar requires the five-step right-only B RTC service')
    policy = Policy(a.bundle, a.rlinf_root)
    from .state_input_profile import install_state_input_profile
    state_input_profile = install_state_input_profile(a.state_input_profile, policy, a.dataset, a.bundle)
    if a.rtc_compile_suffix:
        if not a.rtc:raise ValueError('RTC suffix compilation requires dedicated RTC service')
        from .rtc_optimization import compile_suffix
        compile_suffix(policy)
    prefix = prefix_graph_status(policy)
    if environment_switch('F1_RTC_PREFIX_GRAPH') != prefix['enabled']:
        raise ValueError('Prefix graph requires the validated H20 RTC suffix wrapper')
    if prefix['enabled'] and (not a.rtc or a.steps != 5):
        raise ValueError('Prefix graph is validated only for the dedicated five-step RTC service')
    image_transport = configure_image_transport(policy)
    if image_transport is not None and not a.rtc:
        raise ValueError('Masked image transport requires the dedicated RTC service')
    from .dataset import F1Dataset
    import os
    os.environ["F1_TOYS_TOKENIZER"] = str(Path(a.bundle) / "paligemma_tokenizer.model")
    dataset = F1Dataset(a.dataset)
    with np.load(Path(a.dataset) / "arrays.npz") as arrays:
        held = arrays["submitted"][0,0]
    from .inference_worker import InferenceWorker
    worker = InferenceWorker() if prefix['enabled'] else None
    try:
        rlt_policy = None
        if a.rlt_head is not None:
            from .rlt_inference import RLTInference
            rlt_policy = RLTInference(policy, a.rlt_head, a.rlt_encoder, a.rlt_rlinf_root or a.rlinf_root, rtc=a.rlt_rtc)
            rlt_policy.configure_reference_compute(a.rlt_reference_compute)
            if a.rlt_snapshots:
                rlt_policy.configure_snapshots(a.rlt_snapshots, a.rlt_encoder)
        warm = lambda: warm_policy(policy, dataset, held, steps=a.steps, rtc=a.rtc,
                                   graph_transitions=prefix['enabled'])
        if worker is None:
            warm()
        else:
            worker.run_sync(warm)
        if rlt_policy is not None:
            rlt_warm = lambda: rlt_policy.warm(dataset.raw_images(0), dataset.states[0], held)
            if worker is None:
                rlt_warm()
            else:
                worker.run_sync(rlt_warm)
        server = Server(policy, steps=a.steps, log=a.log, rtc=a.rtc,
                        state_input_profile=state_input_profile, image_transport=image_transport,
                        inference_worker=worker, rlt_policy=rlt_policy)
        app = web.Application(client_max_size=12*1024**2)
        app.router.add_get("/health", server.health)
        app.router.add_get("/ws", server.websocket)
        app.router.add_get("/echo", server.websocket)
        if rlt_policy is not None:
            app.router.add_get("/rlt/health", server.rlt_health)
            app.router.add_get("/rlt/candidates", server.rlt_candidate)
            app.router.add_get("/rlt/features", server.websocket)
            app.router.add_get("/rlt/infer", server.websocket)
        app.on_cleanup.append(server.close)
        print(json.dumps(dict(ready=True, model_id=server.identity, port=a.port,
                              rtc_prefix_graph=prefix_graph_status(policy)), allow_nan=False), flush=True)
        web.run_app(app, host=a.host, port=a.port)
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    main()
