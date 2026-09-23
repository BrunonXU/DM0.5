"""Multipart HTTP transport patterned after DM0.5, with the F1-only contract."""
from __future__ import annotations

from io import BytesIO
import json
import threading
import time

import numpy as np
from PIL import Image, UnidentifiedImageError

from f1_data.reader import CAMERAS, require
from . import CONTRACT


def validate_observation(metadata):
    require(isinstance(metadata, dict) and metadata.get('contract') == CONTRACT, 'F1 inference contract required')
    request_id = metadata.get('request_id')
    require(isinstance(request_id, str) and 0 < len(request_id) <= 128, 'nonempty string request_id required')
    stamp = metadata.get('observation_monotonic_ns')
    require(type(stamp) is int and stamp > 0, 'positive client observation_monotonic_ns required')
    state = metadata.get('state19')
    require(isinstance(state, list) and len(state) == 19 and all(type(v) in (int, float) for v in state),
            'physical state19 numeric list required')
    state = np.asarray(state, dtype=float)
    require(np.isfinite(state).all(), 'finite state19 required')
    require('state_mask' not in metadata and 'state_mask19' not in metadata, 'online inference requires all 19 feedback dimensions')
    reference = metadata.get('reference_joints14_deg', state[:14].tolist())
    require(isinstance(reference, list) and len(reference) == 14 and all(type(v) in (int, float) for v in reference),
            'numeric reference_joints14_deg required')
    reference = np.asarray(reference, dtype=float)
    require(np.isfinite(reference).all(), 'finite reference_joints14_deg required')
    require(((state[14:16] >= 0) & (state[14:16] <= 100)).all(), 'gripper feedback must be in [0,100]')
    result = {'contract': CONTRACT, 'request_id': request_id, 'observation_monotonic_ns': stamp,
              'state19': state.tolist(), 'reference_joints14_deg': reference.tolist()}
    if 'seed' in metadata:
        require(type(metadata['seed']) is int and 0 <= metadata['seed'] < 2**32,
                'seed must be an integer in [0,2^32)')
        result['seed'] = metadata['seed']
    return result


def decode_images(payloads):
    require(set(payloads) == set(CAMERAS), 'exactly head/left/right images required')
    result = {}
    for name in CAMERAS:
        require(isinstance(payloads[name], bytes) and payloads[name], f'empty {name} image')
        try:
            with Image.open(BytesIO(payloads[name])) as source:
                require(source.width * source.height <= 16000000, 'camera image exceeds 16 megapixels')
                source.load()
                result[name] = source.convert('RGB')
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError(f'invalid {name} compressed image') from exc
    return result


def action_response(observation, solved, binding):
    response = {**observation, 'anchor_joints14_deg': observation['state19'][:14],
                'model_binding': binding, 'ik_recipe_version': 'f1_ikfk_demo_v1',
                'execute_steps': 20, 'frequency_hz': 30, 'horizon': 50,
                'status': 'ready' if solved.get('success') is True else 'rejected'}
    if 'inference_diagnostics' in solved:
        response['diagnostics'] = solved['inference_diagnostics']
    if response['status'] == 'rejected':
        # Never expose partial arm/gripper/base commands for a failed prefix.
        response['reason'] = str(solved.get('reason', 'ik_rejected'))
        response['ik'] = {key: solved[key] for key in ('step', 'arm') if key in solved}
        return response
    for key, width in (('joint_targets_deg', 14), ('gripper_targets', 2), ('base_velocity_targets', 3)):
        values = np.asarray(solved.get(key), dtype=float)
        require(values.shape == (20, width) and np.isfinite(values).all(), f'invalid decoded {key}')
        response[key] = values.tolist()
    return response


def create_app(policy, binding, *, max_request_bytes=16777216):
    from flask import Flask, jsonify, request
    from werkzeug.exceptions import RequestEntityTooLarge

    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = max_request_bytes
    inference_lock = threading.Lock()

    @app.get('/health')
    def health():
        return jsonify(contract=CONTRACT, ready=policy is not None, model_binding=binding,
                       horizon=50, execute_steps=20, frequency_hz=30)

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return jsonify(contract=CONTRACT, status='error', error='request too large'), 413

    @app.post('/process_frame')
    def process_frame():
        if policy is None:
            return jsonify(contract=CONTRACT, status='error', error='model not loaded'), 503
        # No queue of old observations; the client has only one request in flight.
        if not inference_lock.acquire(blocking=False):
            return jsonify(contract=CONTRACT, status='error', error='inference busy'), 409
        try:
            started = time.monotonic()
            require(set(request.form) == {'metadata'} and len(request.form.getlist('metadata')) == 1,
                    'exactly one metadata JSON form field required')
            observation = validate_observation(json.loads(request.form['metadata']))
            require(set(request.files) == set(CAMERAS) and all(len(request.files.getlist(c)) == 1 for c in CAMERAS),
                    'exactly one file for each head/left/right camera required')
            images = decode_images({name: request.files[name].read() for name in CAMERAS})
            parsed = time.monotonic()
            solved = policy.predict(observation, images)
            response = action_response(observation, solved, binding)
            response['timing_seconds'] = {'request_parse': parsed - started,
                                          'model_and_ik': time.monotonic() - parsed,
                                          'server_total': time.monotonic() - started}
            return jsonify(response)
        except (ValueError, TypeError, OverflowError) as exc:
            return jsonify(contract=CONTRACT, status='error', error=str(exc)), 400
        except Exception:
            app.logger.exception('F1 inference request failed')
            return jsonify(contract=CONTRACT, status='error', error='inference failed; see server log'), 500
        finally:
            inference_lock.release()

    return app
