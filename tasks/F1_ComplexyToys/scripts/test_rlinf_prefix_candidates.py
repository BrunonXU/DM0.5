"""CPU checks for masked-view cache safety and scoped prefix constant patches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'src'))
from benchmark_rlinf_offline import MaskedImageCache
from rlinf_prefix_candidate import install_prefix_candidate


class Vision(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = False

    def forward(self, pixels):
        self.calls.append(pixels.clone())
        if self.fail:
            raise RuntimeError('deliberate vision failure')
        return pixels * 2. + .25, None


class ImageModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.img = Vision()
        self.eval()

    def embed_prefix(self, observation):
        return {name: self.img(pixels)[0] for name, pixels in observation.images.items()}


def observation(*, inactive_value=1., masked=True, active_value=2.):
    return SimpleNamespace(
        images={'masked_view': torch.full((1, 2, 2, 3), inactive_value),
                'active_view': torch.full((1, 2, 2, 3), active_value)},
        image_masks={'masked_view': torch.tensor([not masked]),
                     'active_view': torch.tensor([True])})


class Attention(torch.nn.Module):
    def forward(self, logits, mask):
        big_neg = -2.3819763e38
        return torch.where(mask, logits, torch.tensor(big_neg, dtype=logits.dtype, device=logits.device))


class PrefixModel:
    def __init__(self):
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


class PrefixTests(unittest.TestCase):
    def setUp(self):
        self.model = ImageModel()
        self.reference = ImageModel()
        self.cache = MaskedImageCache(self.model)

    def assert_matches_reference(self, obs):
        actual = self.cache(obs)
        expected = self.reference.embed_prefix(obs)
        self.assertEqual(actual.keys(), expected.keys())
        for key in actual:
            torch.testing.assert_close(actual[key], expected[key], rtol=0, atol=0)

    def test_active_views_always_evaluate_even_if_pixels_repeat(self):
        with torch.no_grad():
            self.assert_matches_reference(observation(masked=False))
            self.assert_matches_reference(observation(masked=False))
        self.assertEqual(len(self.model.img.calls), 4)
        self.assertEqual(self.cache.hits, 0)

    def test_active_alias_of_masked_tensor_is_never_cached(self):
        obs = observation()
        obs.images['active_view'] = obs.images['masked_view']
        with torch.no_grad():
            self.assert_matches_reference(obs)
            self.assert_matches_reference(obs)
        self.assertEqual(len(self.model.img.calls), 4)
        self.assertEqual(self.cache.hits, 0)

    def test_only_identical_inactive_view_reuses_result(self):
        with torch.no_grad():
            self.assert_matches_reference(observation())
            self.assert_matches_reference(observation(active_value=3.))
        self.assertEqual(len(self.model.img.calls), 3)
        self.assertEqual(self.cache.hits, 1)
        self.assertTrue(torch.equal(self.model.img.calls[-1], observation(active_value=3.).images['active_view']))

    def test_mask_change_to_active_forces_fresh_execution(self):
        with torch.no_grad():
            self.assert_matches_reference(observation())
            self.assert_matches_reference(observation(masked=False))
            self.assert_matches_reference(observation(masked=True))
        self.assertEqual(len(self.model.img.calls), 5)
        self.assertEqual(self.cache.hits, 1)

    def test_changed_inactive_pixels_are_recomputed(self):
        with torch.no_grad():
            self.assert_matches_reference(observation())
            self.assert_matches_reference(observation(inactive_value=7.))
            self.assert_matches_reference(observation(inactive_value=7.))
        self.assertEqual(len(self.model.img.calls), 5)
        self.assertEqual(self.cache.hits, 1)

    def test_vision_forward_is_restored_after_exception(self):
        original = self.model.img.forward
        self.model.img.fail = True
        with torch.no_grad(), self.assertRaisesRegex(RuntimeError, 'deliberate vision failure'):
            self.cache(observation())
        self.assertEqual(self.model.img.forward, original)
        self.model.img.fail = False
        with torch.no_grad():
            self.assert_matches_reference(observation())
        self.assertEqual(self.model.img.forward, original)

    def test_cache_rejects_training_and_grad_enabled_use(self):
        with self.assertRaisesRegex(RuntimeError, 'frozen inference-only'):
            self.cache(observation())
        self.model.train()
        with torch.no_grad(), self.assertRaisesRegex(RuntimeError, 'frozen inference-only'):
            self.cache(observation())

    def test_constant_patch_is_exact_instance_local_and_restorable(self):
        model, other = PrefixModel(), PrefixModel()
        policy = SimpleNamespace(model=model)
        obs = SimpleNamespace(tokens=torch.tensor([.4, -.8]), mask=torch.tensor([True, False]))
        prefix, embed, attention = model.build_prefix_cache, model.embed_prefix, model.llm.layers[0].attn.forward
        expected = prefix(obs)
        with install_prefix_candidate(policy, 'constants'):
            actual = model.build_prefix_cache(obs)
            for x, y in zip(actual, expected):
                torch.testing.assert_close(x, y, rtol=0, atol=0)
            self.assertEqual(model.embed_prefix, embed)
            self.assertEqual(model.llm.layers[0].attn.forward, attention)
            self.assertNotIn('embed_prefix', vars(model))
            self.assertNotIn('forward', vars(model.llm.layers[0].attn))
            self.assertIs(other.embed_prefix.__func__, embed.__func__)
        self.assertEqual(model.build_prefix_cache, prefix)
        self.assertNotIn('build_prefix_cache', vars(model))

    def test_constant_patch_restores_submodules_and_records_full_exception(self):
        model = PrefixModel()
        policy = SimpleNamespace(model=model)
        obs = SimpleNamespace(tokens=torch.ones(2), mask=torch.ones(2, dtype=torch.bool))
        original_embed, original_attention = model.embed_prefix, model.llm.layers[0].attn.forward
        model.fail = True
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'trace.txt'
            with install_prefix_candidate(policy, 'constants', error_path=trace):
                with self.assertRaisesRegex(RuntimeError, 'deliberate prefix failure'):
                    model.build_prefix_cache(obs)
                self.assertEqual(model.embed_prefix, original_embed)
                self.assertEqual(model.llm.layers[0].attn.forward, original_attention)
                self.assertIn('Traceback (most recent call last)', trace.read_text())
                self.assertIn('deliberate prefix failure', trace.read_text())

    def test_graph_no_cache_mode_preserves_amp_dtype_and_restores_context(self):
        policy = SimpleNamespace(model=PrefixModel())
        prior_cache = torch.is_autocast_cache_enabled()
        prior_enabled = torch.is_autocast_enabled('cuda')
        prior_dtype = torch.get_autocast_dtype('cuda')
        prior_inference_mode = torch.is_inference_mode_enabled()
        with install_prefix_candidate(policy, 'graph_no_autocast_cache') as candidate:
            self.assertEqual(type(candidate.selected).__name__, 'EagerPrefixGraph')
            self.assertFalse(candidate.debug_sync)
            observed = []
            def probe(obs):
                observed.append((torch.is_autocast_cache_enabled(),
                                 torch.is_autocast_enabled('cuda'),
                                 torch.get_autocast_dtype('cuda'),
                                 torch.is_inference_mode_enabled()))
                return 'prefix-result'
            candidate.selected = probe  # No CUDA graph is executed by this CPU test.
            self.assertEqual(candidate(None), 'prefix-result')
            self.assertEqual(observed, [(False, prior_enabled, prior_dtype, prior_inference_mode)])
            self.assertEqual(torch.is_autocast_cache_enabled(), prior_cache)
            self.assertEqual(torch.is_autocast_enabled('cuda'), prior_enabled)
            self.assertEqual(torch.get_autocast_dtype('cuda'), prior_dtype)
            self.assertEqual(candidate.last_stage, 'completed')

    def test_graph_no_cache_mode_restores_amp_after_prefix_exception(self):
        policy = SimpleNamespace(model=PrefixModel())
        prior_cache = torch.is_autocast_cache_enabled()
        def fail(obs):
            self.assertFalse(torch.is_autocast_cache_enabled())
            raise RuntimeError('AMP ownership diagnostic')
        with install_prefix_candidate(policy, 'graph_no_autocast_cache') as candidate:
            candidate.selected = fail
            with self.assertRaisesRegex(RuntimeError, 'AMP ownership diagnostic'):
                candidate(None)
            self.assertEqual(torch.is_autocast_cache_enabled(), prior_cache)
            self.assertEqual(candidate.last_stage, 'prefix')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PrefixTests))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            'tests_run': result.testsRun, 'failures': [(str(t), e) for t, e in result.failures],
            'errors': [(str(t), e) for t, e in result.errors], 'success': result.wasSuccessful(),
            'torch_version': torch.__version__, 'device': 'cpu', 'cuda_used': False,
        }, indent=2) + '\n')
    sys.exit(0 if result.wasSuccessful() else 1)
