"""CPU-only recovery selection, immutable input and full-VJP cache tests."""
import contextlib
import copy
import importlib.util
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import ik_recovery_server as recovery


@contextlib.contextmanager
def fake_cache(model):
    yield dict(builds=1, hits=0, freeze=lambda: None)


def rejected(n, value):
    return dict(success=False, actions=None, reason='ik_rejected',
        absolute_eef10=[[value]*10]*50, timing_ms={'policy': 1.},
        diagnostics=[dict(success=True, joints=[float(value)]*7) for _ in range(n)] +
        [dict(success=False, joints=None)])


def successful(value):
    return dict(success=True, actions=[[value]*16]*42, absolute_eef10=[[value]*10]*50,
                diagnostics=[], timing_ms={'policy': 1.})


class SelectionTests(unittest.TestCase):
    def run_candidates(self, responses, costs=None, budget=400., max_attempts=4):
        now = [0.]
        calls, audits = [], []
        source = ({'image': [1, 2]}, [3], [4], {'committed_actions16': [[5]*16]})
        pristine = copy.deepcopy(source)
        raw_buffer = [None]
        def predict(*args, **kwargs):
            self.assertEqual(args, pristine)
            i = len(calls); calls.append(kwargs.get('seed'))
            args[0]['image'][0] = 900
            args[3]['committed_actions16'][0][0] = 900
            now[0] += (costs[i] if costs else 20.) / 1000
            raw_buffer[0] = i
            return copy.deepcopy(responses[i]), raw_buffer, {'candidate': i}
        out = recovery.recover_predict(SimpleNamespace(model=None), predict, source, {'seed': 90},
            clock=lambda: now[0], seed_factory=lambda: 100, cache_factory=fake_cache,
            budget_ms=budget, max_attempts=max_attempts, audit=audits.append)
        self.assertEqual(source, pristine)
        return out, calls, audits

    def test_original_success_unchanged_and_single_call(self):
        expected = successful(1)
        out, calls, logs = self.run_candidates([expected], costs=[500.])
        self.assertEqual(out[0], expected)
        self.assertEqual(calls, [90])
        self.assertEqual(logs[0]['stop'], 'original_success')

    def test_first_strict_success_uses_matching_raw_data(self):
        out, calls, logs = self.run_candidates([rejected(4, 0), successful(1), successful(2)])
        self.assertEqual(calls, [90, 100])
        self.assertTrue(out[0]['success'])
        self.assertEqual(out[1:], ([1], {'candidate': 1}))
        self.assertEqual(out[0]['ik_recovery']['selected_order'], 1)
        self.assertTrue(out[0]['ik_recovery']['resampled_prediction_selected'])
        self.assertFalse(out[0]['ik_recovery']['selected_original_prediction'])
        self.assertFalse(out[0]['ik_recovery']['eef_postprocessed'])
        self.assertNotIn('target_changed', out[0]['ik_recovery'])

    def test_longest_rejected_prefix_and_no_alias(self):
        out, calls, _ = self.run_candidates([rejected(2, 0), rejected(9, 1), rejected(4, 2), rejected(9, 3)])
        self.assertEqual(calls, [90, 100, 101, 102])
        self.assertFalse(out[0]['success']); self.assertIsNone(out[0]['actions'])
        self.assertEqual(out[0]['reason'], 'ik_rejected')
        self.assertEqual(out[1:], ([1], {'candidate': 1}))
        self.assertEqual(out[0]['absolute_eef10'][0][0], 1)
        self.assertEqual(out[0]['ik_recovery']['valid_suffix_steps'], 9)

    def test_overbudget_success_excluded(self):
        out, calls, _ = self.run_candidates([rejected(2, 0), successful(1)], costs=[50., 100.], budget=100.)
        self.assertFalse(out[0]['success'])
        self.assertEqual(out[1], [0])
        self.assertTrue(out[0]['ik_recovery']['selected_original_prediction'])
        self.assertFalse(out[0]['ik_recovery']['resampled_prediction_selected'])
        self.assertEqual(out[0]['ik_recovery']['stop'], 'selection_budget_after_attempt')
        self.assertFalse(out[0]['ik_recovery']['attempts'][1]['eligible'])

    def test_baseline_exhausts_budget_no_retry(self):
        out, calls, _ = self.run_candidates([rejected(2, 0)], costs=[101.], budget=100.)
        self.assertEqual(calls, [90])
        self.assertEqual(out[0]['ik_recovery']['stop'], 'selection_budget_before_attempt')

    def test_cache_clone_cost_counts_toward_budget(self):
        now, calls = [0.], []
        @contextlib.contextmanager
        def cache(model):
            def freeze(): now[0] += .09
            yield dict(builds=1, hits=0, freeze=freeze)
        def predict(*args, **kwargs):
            calls.append(1); now[0] += .02
            return rejected(2, 0), [0], {}
        out = recovery.recover_predict(SimpleNamespace(model=None), predict, (), {},
            clock=lambda: now[0], cache_factory=cache, budget_ms=100.)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out[0]['ik_recovery']['stop'], 'selection_budget_before_attempt')
        self.assertGreater(out[0]['ik_recovery']['total_ms'], 100.)

    def test_later_non_ik_failure_is_not_hidden_even_after_budget(self):
        for second_cost in (10., 1000.):
            with self.assertRaisesRegex(RuntimeError, 'Non-IK recovery candidate failure'):
                self.run_candidates([rejected(8, 0),
                    dict(success=False, actions=None, reason='backend_failure')],
                    costs=[20., second_cost])

    def test_prediction_exception_propagates(self):
        calls = []
        def predict(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1: return rejected(8, 0), [0], {}
            raise ValueError('sampler exploded')
        with self.assertRaisesRegex(ValueError, 'sampler exploded'):
            recovery.recover_predict(SimpleNamespace(model=None), predict, (), {},
                cache_factory=fake_cache)

    def test_non_ik_error_not_retried(self):
        out, calls, _ = self.run_candidates([dict(success=False, actions=None, reason='other', timing_ms={})])
        self.assertEqual(calls, [90])
        self.assertEqual(out[0]['reason'], 'other')


@unittest.skipUnless(importlib.util.find_spec('torch'), 'torch unavailable in host environment')
class CacheVJPTests(unittest.TestCase):
    def test_full_sampler_vjp_bitwise_and_independent_cache(self):
        import torch
        from f1_toys.rtc import sample_actions
        class Model:
            action_horizon, action_dim = 50, 2
            def __init__(self):
                self.builds = 0
                self.matrix = torch.tensor([[.4, -.2], [.7, .1]])
            def build_prefix_cache(self, obs):
                self.builds += 1
                return None, torch.ones((1, 1), dtype=torch.bool), {'k': obs.state * .3}
            def mask_action_coordinates(self, x): return x
            def run_suffix(self, obs, value, times, cache, mask):
                return torch.sin(value @ self.matrix + cache['k'][:, None, :])
            def velocity_from_suffix(self, hidden): return hidden
        model = Model()
        observation = SimpleNamespace(state=torch.tensor([[.2, -.4]]))
        target = torch.ones((1, 50, 2)) * .15
        def predict(seed=3):
            return sample_actions(model, observation, lambda x, train: x,
                target=target, delay=14, shift=16, steps=5,
                rng=torch.Generator().manual_seed(seed))
        grad = torch.autograd.grad
        with patch.object(torch.autograd, 'grad', wraps=grad) as counted:
            baseline = predict()
            original = model.build_prefix_cache
            with recovery.request_prefix_cache(model) as state:
                first = predict()
                original_tensors = list(recovery.tensor_leaves(state['original_cache']))
                state['freeze']()
                cached = list(recovery.tensor_leaves(state['cache']))
                before = [t.clone() for t in cached]
                self.assertTrue(all(a.data_ptr() != b.data_ptr() for a, b in zip(original_tensors, cached)))
                hit = predict()
                torch.testing.assert_close(baseline, first, rtol=0, atol=0)
                torch.testing.assert_close(first, hit, rtol=0, atol=0)
                self.assertEqual(state['builds'], 1); self.assertEqual(state['hits'], 1)
                self.assertTrue(all(torch.equal(a, b) for a, b in zip(before, cached)))
                self.assertTrue(all(not t.requires_grad and t.grad_fn is None for t in cached))
                cached[-1].add_(1.)
                with self.assertRaisesRegex(RuntimeError, 'cache mutated'):
                    predict()
            self.assertEqual(model.build_prefix_cache, original)
            self.assertEqual(counted.call_count, 15)
        self.assertEqual(model.builds, 2)

    def test_actual_health_marks_independent_wrapper_without_service(self):
        import asyncio
        import json
        from f1_toys.server import Server
        class IsolatedServer(Server):
            pass
        policy = SimpleNamespace(bundle_id='a'*64)
        server = IsolatedServer(policy, rtc=True)
        before = asyncio.run(server.health(None))
        recovery.install_health(IsolatedServer, max_attempts=3, budget_ms=350.)
        after = asyncio.run(server.health(None))
        body = json.loads(after.text)
        identity = body.pop('ik_recovery')
        self.assertEqual(body, json.loads(before.text))
        self.assertEqual(identity['wrapper'], 'f1_strict_ik_recovery_v1')
        self.assertEqual(identity['max_attempts'], 3)
        self.assertEqual(identity['budget_ms'], 350.)
        self.assertTrue(identity['enabled'])
        self.assertTrue(identity['may_select_resampled_prediction'])
        self.assertEqual(len(identity['wrapper_sha256']), 64)
        self.assertEqual(after.content_type, 'application/json')
        server.rtc = False
        self.assertFalse(json.loads(asyncio.run(server.health(None)).text)['ik_recovery']['enabled'])

    def test_changed_observation_rejected_and_method_restored(self):
        import torch
        model = SimpleNamespace(build_prefix_cache=lambda obs: (None, None, {'k': obs.state + 1}))
        observation = SimpleNamespace(state=torch.tensor([[1.]]))
        original = model.build_prefix_cache
        with recovery.request_prefix_cache(model) as state:
            model.build_prefix_cache(observation); state['freeze']()
            observation.state.add_(1)
            with self.assertRaisesRegex(RuntimeError, 'Observation changed'):
                model.build_prefix_cache(observation)
        self.assertEqual(model.build_prefix_cache, original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
