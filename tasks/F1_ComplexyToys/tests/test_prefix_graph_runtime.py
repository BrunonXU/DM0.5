"""CPU contract/lifecycle tests; CUDA numerical parity is validated separately."""
import asyncio
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import torch

from f1_toys.inference_worker import InferenceWorker
from f1_toys.input_optimization import (MaskedLeftTransport, configure_image_transport,
                                        environment_switch)
from f1_toys.rtc_prefix_optimization import (PrefixGraphOptimization, prepare_prefix_graph,
                                            prefix_graph_status)
from f1_toys.server import Server, unpack, warm_policy


def contract():
    return dict(schema='f1_toys_eef_contract_v2', input_version='right_only_v1',
                active_cameras=['head', 'right_wrist'], cameras=['head', 'left_wrist', 'right_wrist'],
                active_state_indices=[7, 8, 9, 10, 11, 12, 13, 15],
                active_action_indices=list(range(10)), model_action_dim=32, horizon=50,
                image_size=224, mask_inactive_action_inputs=True)


class Attention(torch.nn.Module):
    def forward(self, logits, mask):
        big_neg = -2.3819763e38
        return torch.where(mask, logits, torch.tensor(big_neg, dtype=logits.dtype, device=logits.device))


class PrefixModel:
    training = False
    action_horizon = 50
    action_dim = 32

    def __init__(self):
        self._config = SimpleNamespace(action_loss_mask=[True] * 10 + [False] * 22)
        self.llm = SimpleNamespace(layers=[SimpleNamespace(attn=Attention())])
        self.fail = False

    def embed_prefix(self, observation):
        tokens = observation.tokens
        ar_mask = [False] * tokens.shape[-1]
        ar_mask = torch.tensor(ar_mask, device=tokens.device)
        return tokens, ar_mask

    def build_prefix_cache(self, observation):
        tokens, ar_mask = self.embed_prefix(observation)
        if self.fail:
            raise RuntimeError('deliberate prefix failure')
        return self.llm.layers[0].attn(tokens, observation.mask), ar_mask


def prefix_policy():
    return SimpleNamespace(model=PrefixModel(), contract=contract(), device='cuda')


class FakePolicy:
    bundle_id = 'fake-right-only-cpu-policy'

    def __init__(self):
        self.contract = contract()
        self.input_preprocessing = dict(contract_sha256='a' * 64, implementation_sha256='b' * 64)
        self.calls = []

    @torch.inference_mode()
    def predict_eef(self, images, state, held, **kwargs):
        self.calls.append(('sync', threading.get_ident(), torch.is_inference_mode_enabled()))

    def predict_rtc(self, images, state, held, rtc, **kwargs):
        self.calls.append(('bootstrap' if rtc['bootstrap'] else 'guided',
                           threading.get_ident(), torch.is_inference_mode_enabled()))
        self.last = (images, deepcopy(state), deepcopy(held), deepcopy(rtc), kwargs)
        return dict(success=True, actions=[list(held)] * 32, absolute_eef10=[[0.] * 10] * 50,
                    timing_ms={}), np.zeros((50, 32), np.float32), None


def wire_payload(policy, *, marker=False, bad_left=False):
    constant = Path(__file__).with_name('fixtures').joinpath('masked-left.png').read_bytes()
    frames, data = [], []
    for camera in ('head', 'left_wrist', 'right_wrist'):
        stream = BytesIO()
        Image.new('RGB', (224, 224), (32, 64, 96)).save(stream, format='PNG')
        raw = constant if camera == 'left_wrist' and not bad_left else stream.getvalue()
        frames.append(dict(camera=camera, encoding='image/png', width=224, height=224, length=len(raw)))
        data.append(raw)
    meta = dict(protocol='f1_eef_rtc_v2', contract='f1_upper_16d_v1',
                prediction_horizon=50, execute_steps=20, chunk_steps=20, frequency_hz=30,
                request_id=1, observation_id=1, policy_epoch=0, observation_monotonic_ns=1,
                session_id='cpu-test', state16=list(range(16)), last_submitted16=list(range(16)),
                prepared_pixels=policy.input_preprocessing, frames=frames,
                model_id='f1-toys-eef-' + policy.bundle_id[:16], model_version=policy.bundle_id,
                rtc=dict(bootstrap=True, delay_steps=14, shift_steps=16, coverage_steps=32,
                         plan_origin_ns=0, base_plan_id=0, committed_actions16=[], previous_absolute_eef10=[]))
    if marker:
        meta['image_transport'] = MaskedLeftTransport(policy).identity if marker is True else marker
    encoded = json.dumps(meta).encode()
    return struct.pack('!I', len(encoded)) + encoded + b''.join(data)


class PrefixRuntimeTests(unittest.TestCase):
    def test_default_switches_leave_an_unrelated_policy_untouched(self):
        policy = SimpleNamespace()
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(prepare_prefix_graph(policy))
            self.assertIsNone(configure_image_transport(policy))
        self.assertEqual(vars(policy), {})
        self.assertEqual(prefix_graph_status(policy), dict(enabled=False, mode='eager', graph_ready=False))

    def test_invalid_switch_is_not_silently_disabled(self):
        with patch.dict('os.environ', {'F1_RTC_PREFIX_GRAPH': 'true'}):
            with self.assertRaisesRegex(ValueError, '0 or 1'):
                environment_switch('F1_RTC_PREFIX_GRAPH')

    def test_contract_and_mask_rejection_are_atomic(self):
        for field, value in [('input_version', 'legacy'), ('active_cameras', ['head', 'left_wrist', 'right_wrist'])]:
            policy = prefix_policy()
            original = policy.model.build_prefix_cache
            policy.contract[field] = value
            with self.assertRaisesRegex(ValueError, 'right_only_v1'):
                PrefixGraphOptimization(policy)
            self.assertEqual(policy.model.build_prefix_cache, original)
            self.assertFalse(hasattr(policy, 'rtc_prefix_graph'))
        policy = prefix_policy()
        policy.model._config.action_loss_mask = [True] * 32
        with self.assertRaisesRegex(ValueError, 'action mask'):
            PrefixGraphOptimization(policy)
        self.assertNotIn('build_prefix_cache', vars(policy.model))

    def test_source_validation_precedes_any_installation(self):
        policy = prefix_policy()
        policy.model.llm.layers.append(SimpleNamespace(attn=torch.nn.Identity()))
        with self.assertRaisesRegex(ValueError, 'validated frozen-source constant'):
            PrefixGraphOptimization(policy)
        self.assertNotIn('embed_prefix', vars(policy.model))
        self.assertNotIn('forward', vars(policy.model.llm.layers[0].attn))
        self.assertNotIn('build_prefix_cache', vars(policy.model))
        self.assertFalse(hasattr(policy, 'rtc_prefix_graph'))

    def test_inference_mode_uses_exact_eager_path_and_restores_attributes(self):
        policy = prefix_policy()
        obs = SimpleNamespace(tokens=torch.tensor([.4, -.8]), mask=torch.tensor([True, False]))
        original = policy.model.build_prefix_cache
        expected = original(obs)
        handle = PrefixGraphOptimization(policy).install()
        with torch.inference_mode():
            actual = policy.model.build_prefix_cache(obs)
        for a, b in zip(actual, expected):
            torch.testing.assert_close(a, b, rtol=0, atol=0)
        self.assertFalse(handle.status['graph_ready'])
        self.assertEqual(handle.diagnostics['replay_count'], 0)
        self.assertNotIn('embed_prefix', vars(policy.model))
        self.assertNotIn('forward', vars(policy.model.llm.layers[0].attn))
        handle.close()
        self.assertEqual(policy.model.build_prefix_cache, original)
        self.assertNotIn('build_prefix_cache', vars(policy.model))
        self.assertFalse(hasattr(policy, 'rtc_prefix_graph'))

    def test_amp_context_restored_and_ready_only_after_successful_graph_call(self):
        policy = prefix_policy()
        handle = PrefixGraphOptimization(policy).install()
        previous = (torch.is_autocast_cache_enabled(), torch.is_autocast_enabled('cuda'),
                    torch.get_autocast_dtype('cuda'))
        class Probe:
            graph = None
            def __call__(self, obs):
                self.observed = (torch.is_autocast_cache_enabled(), torch.is_autocast_enabled('cuda'),
                                 torch.get_autocast_dtype('cuda'))
                self.graph = object()  # CPU stand-in; this test executes no CUDA graph.
                return 'captured-result'
        handle.selected = Probe()
        self.assertFalse(handle.status['graph_ready'])
        with torch.no_grad():
            self.assertEqual(handle(None), 'captured-result')
        self.assertEqual(handle.selected.observed, (False, previous[1], previous[2]))
        self.assertEqual((torch.is_autocast_cache_enabled(), torch.is_autocast_enabled('cuda'),
                          torch.get_autocast_dtype('cuda')), previous)
        self.assertTrue(handle.status['graph_ready'])
        handle.close()

    def test_failure_is_recorded_restored_and_never_falls_back(self):
        policy = prefix_policy()
        policy.model.fail = True
        handle = PrefixGraphOptimization(policy).install()
        obs = SimpleNamespace(tokens=torch.ones(2), mask=torch.ones(2, dtype=torch.bool))
        cache_before = torch.is_autocast_cache_enabled()
        with torch.inference_mode(), self.assertRaisesRegex(RuntimeError, 'deliberate prefix failure'):
            handle(obs)
        self.assertEqual(torch.is_autocast_cache_enabled(), cache_before)
        self.assertIn('Traceback (most recent call last)', handle.last_traceback)
        self.assertNotIn('embed_prefix', vars(policy.model))
        self.assertNotIn('forward', vars(policy.model.llm.layers[0].attn))
        self.assertTrue(handle.status['failed'])
        self.assertFalse(handle.status['graph_ready'])
        policy.model.fail = False
        with self.assertRaisesRegex(RuntimeError, 'restart'):
            handle(obs)
        handle.close()

    def test_worker_warmup_and_requests_share_thread_and_close_cleanly(self):
        policy = FakePolicy()
        policy.rtc_prefix_graph = SimpleNamespace(status=dict(enabled=True, graph_ready=True))
        worker = InferenceWorker()
        dataset = SimpleNamespace(raw_images=lambda _: {}, states=[np.arange(16)])
        try:
            worker.run_sync(warm_policy, policy, dataset, np.arange(16), steps=5, rtc=True, graph_transitions=True)
            asyncio.run(worker.run(policy.predict_rtc, {}, list(range(16)), list(range(16)), {'bootstrap': True}))
            self.assertEqual([x[0] for x in policy.calls], ['sync', 'sync', 'bootstrap', 'guided', 'bootstrap', 'guided', 'bootstrap'])
            self.assertEqual([x[2] for x in policy.calls], [True, True, False, False, False, False, False])
            self.assertEqual(len({x[1] for x in policy.calls}), 1)
            self.assertNotEqual(policy.calls[0][1], threading.get_ident())
            server = Server(policy, inference_worker=worker)
            asyncio.run(server.close())
            self.assertTrue(worker.closed)
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                worker.run_sync(lambda: None)
        finally:
            worker.close()

    def test_original_warmup_has_no_added_graph_transition_calls(self):
        policy = FakePolicy()
        dataset = SimpleNamespace(raw_images=lambda _: {}, states=[np.arange(16)])
        warm_policy(policy, dataset, np.arange(16), steps=5, rtc=True)
        self.assertEqual([x[0] for x in policy.calls], ['sync', 'sync', 'bootstrap', 'guided'])

    def test_health_reports_actual_ready_state_and_transport_capability(self):
        policy = FakePolicy()
        policy.rtc_prefix_graph = SimpleNamespace(status=dict(enabled=True, graph_ready=False))
        server = Server(policy, rtc=True, image_transport=MaskedLeftTransport(policy))
        health = json.loads(asyncio.run(server.health(None)).body)
        self.assertFalse(health['ready'])
        self.assertFalse(health['rtc_prefix_graph']['graph_ready'])
        self.assertEqual(health['image_transport'], server.image_transport.identity)
        policy.rtc_prefix_graph.status['graph_ready'] = True
        self.assertTrue(json.loads(asyncio.run(server.health(None)).body)['ready'])

    def test_transport_requires_opt_in_and_rejects_wrong_identity_or_bytes(self):
        policy = FakePolicy()
        transport = MaskedLeftTransport(policy)
        kwargs = dict(rtc_enabled=True, preprocessing=policy.input_preprocessing)
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            unpack(wire_payload(policy, marker=True), **kwargs)
        for marker in [dict(transport.identity, contract_sha256='c' * 64), dict(transport.identity, mode='other')]:
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                unpack(wire_payload(policy, marker=marker), image_transport=transport, **kwargs)
        with self.assertRaisesRegex(ValueError, 'PNG bytes'):
            unpack(wire_payload(policy, marker=True, bad_left=True), image_transport=transport, **kwargs)
        meta, images = unpack(wire_payload(policy, marker=True), image_transport=transport, **kwargs)
        self.assertEqual(meta['image_transport'], transport.identity)
        self.assertTrue(np.all(np.asarray(images['left_wrist']) == 0))

    def test_transport_never_claims_unmarked_requests_and_none_profile_preserves_input(self):
        policy = FakePolicy()
        transport = MaskedLeftTransport(policy)
        server = Server(policy, steps=5, rtc=True, image_transport=transport, state_input_profile=None)
        legacy = server.compute(wire_payload(policy, bad_left=True), False)
        self.assertNotIn('image_transport', legacy)
        self.assertNotIn('state_input_profile', legacy)
        self.assertEqual(policy.last[1], list(range(16)))
        self.assertEqual(policy.last[2], list(range(16)))
        np.testing.assert_array_equal(np.asarray(policy.last[0]['left_wrist'])[0, 0], [32, 64, 96])
        optimized = server.compute(wire_payload(policy, marker=True), False)
        self.assertEqual(optimized['image_transport'], transport.identity)
        self.assertEqual(policy.last[1], list(range(16)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
