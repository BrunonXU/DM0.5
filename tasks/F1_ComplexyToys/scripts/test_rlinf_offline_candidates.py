"""CPU acceptance for candidate mathematics and per-policy isolation.

Run from any directory with the established task torch environment. No model
weights, service, CUDA context or robot connection is required. Reference tests
execute functions extracted from the pinned upstream source, not a second local
implementation of the formula.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import torch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys import rtc
from rlinf_offline_candidates import install_rtc_candidate, sample_actions_candidate


def preprocess(observation, train=False):
    assert train is False
    return observation


class LinearModel:
    action_horizon = 50
    action_dim = 32

    def __init__(self):
        self.matrix = torch.zeros(32, 32)
        # Non-diagonal derivatives distinguish I from a transposed full VJP.
        self.matrix[:10, :10] = torch.eye(10) * .35
        self.matrix[0, 1] = .12
        self.matrix[1, 0] = -.08

    def mask_action_coordinates(self, value):
        mask = torch.arange(32, device=value.device) < 10
        return torch.where(mask, value, 0.)

    def build_prefix_cache(self, observation):
        return None, observation.image_masks, None

    def run_suffix(self, observation, value, times, cache, prefix_mask):
        assert prefix_mask is observation.image_masks
        return value @ self.matrix.T + .03 * times[:, None, None]

    def velocity_from_suffix(self, value):
        return self.mask_action_coordinates(value)


def upstream_reference():
    guide = TASK / "artifacts/20261022-framework-audit/rlinf-upstream/rlinf/models/embodiment/openpi/sampling/rtc_guidance.py"
    sampler = TASK / "artifacts/20261023-rlinf-offline/upstream/rl_sampler.py"

    def load_definitions(path, names, namespace):
        tree = ast.parse(path.read_text())
        selected = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        if {node.name for node in selected} != set(names):
            raise AssertionError(f"Pinned source lacks requested definitions: {path}")
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
        return namespace

    sample_ns = load_definitions(sampler, ["get_timesteps", "sample_mean_var"],
                                 {"torch": torch, "math": math})
    ns = load_definitions(guide, ["RTCGuidanceContext", "build_rtc_target_and_mask",
                                  "sample_actions_with_rtc_guidance"],
                          {"torch": torch, "math": math, "dataclass": dataclass,
                           "preprocess_observation": preprocess,
                           "rl_sampler": SimpleNamespace(**sample_ns)})
    return SimpleNamespace(**ns)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.model = LinearModel()
        self.observation = SimpleNamespace(state=torch.zeros(2, 32),
                                           image_masks={"main": True, "left": False, "right": True})
        generator = torch.Generator().manual_seed(20261023)
        self.noise = torch.randn(2, 50, 32, generator=generator)
        self.target = torch.randn(2, 50, 32, generator=generator) * .2

    def sample(self, mode, **overrides):
        kwargs = dict(target=self.target, delay=14, shift=16, steps=5,
                      noise=self.noise, mode=mode)
        kwargs.update(overrides)
        return sample_actions_candidate(self.model, self.observation, preprocess, **kwargs)

    def test_identity_changes_only_jacobian_in_one_step(self):
        x = self.model.mask_action_coordinates(self.noise)
        target = self.model.mask_action_coordinates(self.target)
        v = self.model.mask_action_coordinates(x @ self.model.matrix.T + .03)
        error = (target - (x - v)) * torch.tensor(rtc.soft_mask(50, 14, 16))[None, :, None]
        expected_identity = self.model.mask_action_coordinates(x - v + 5. * error)
        clean_jacobian = torch.eye(32) - self.model.matrix
        expected_vjp = self.model.mask_action_coordinates(x - v + 5. * (error @ clean_jacobian))
        identity, vjp = self.sample("jacobian_identity", steps=1), self.sample("baseline", steps=1)
        torch.testing.assert_close(identity, expected_identity, atol=1e-6, rtol=1e-6)
        torch.testing.assert_close(vjp, expected_vjp, atol=2e-6, rtol=1e-6)
        self.assertGreater(float((identity - vjp).abs().max()), .1)

    def test_identity_bootstrap_exactly_matches_baseline(self):
        torch.testing.assert_close(self.sample("baseline", target=None),
                                   self.sample("jacobian_identity", target=None), rtol=0, atol=0)

    def test_upstream_matches_pinned_source_and_rng_consumption(self):
        reference = upstream_reference()
        for shift, delay, guided in [(16, 14, True), (20, 0, True), (20, 15, True), (16, 14, False)]:
            with self.subTest(shift=shift, delay=delay, guided=guided):
                source_rng, candidate_rng = torch.Generator().manual_seed(7), torch.Generator().manual_seed(7)
                context = reference.RTCGuidanceContext(
                    prev_model_actions=(self.model.mask_action_coordinates(self.target)[:, :50-shift] if guided else None),
                    executed_horizon=0, delay_steps=delay)
                expected = reference.sample_actions_with_rtc_guidance(
                    self.model, self.observation, context, num_steps=5,
                    noise=self.model.mask_action_coordinates(self.noise), rng=source_rng)
                actual = self.sample("upstream_approx", shift=shift, delay=delay,
                                     target=self.target if guided else None, rng=candidate_rng)
                torch.testing.assert_close(actual, expected, rtol=0, atol=0)
                torch.testing.assert_close(candidate_rng.get_state(), source_rng.get_state(), rtol=0, atol=0)

    def test_candidates_need_no_backward_and_preserve_explicit_noise(self):
        original = self.noise.clone()
        for mode in ("jacobian_identity", "upstream_approx"):
            with self.subTest(mode=mode), patch("torch.autograd.grad", side_effect=AssertionError("unexpected VJP")):
                first = self.sample(mode, rng=torch.Generator().manual_seed(1))
                second = self.sample(mode, rng=torch.Generator().manual_seed(99))
                torch.testing.assert_close(first, second, rtol=0, atol=0)
                torch.testing.assert_close(self.noise, original, rtol=0, atol=0)
                self.assertEqual(tuple(first.shape), (2, 50, 32))
                self.assertEqual(int(torch.count_nonzero(first[..., 10:])), 0)
                self.assertTrue(bool(torch.isfinite(first).all()))

    def test_installation_is_per_model_and_restores_original_sampler(self):
        original_sampler = rtc.sample_actions
        policy = SimpleNamespace(model=self.model)
        other_model = LinearModel()
        kwargs = dict(target=self.target, delay=14, shift=16, steps=5)
        with install_rtc_candidate(policy, "jacobian_identity", noise=self.noise) as handle:
            actual = rtc.sample_actions(self.model, self.observation, preprocess, **kwargs)
            torch.testing.assert_close(actual, self.sample("jacobian_identity"), rtol=0, atol=0)
            other = rtc.sample_actions(other_model, self.observation, preprocess, noise=self.noise, **kwargs)
            expected_other = original_sampler(other_model, self.observation, preprocess, noise=self.noise, **kwargs)
            torch.testing.assert_close(other, expected_other, rtol=0, atol=0)
            handle.set_noise(self.noise * .7)
            changed = rtc.sample_actions(self.model, self.observation, preprocess, **kwargs)
            self.assertGreater(float((changed - actual).abs().max()), 0.)
            with self.assertRaises(ValueError):
                install_rtc_candidate(policy, "upstream_approx")
        self.assertIs(rtc.sample_actions, original_sampler)
        with install_rtc_candidate(policy, "baseline", noise=self.noise):
            actual = rtc.sample_actions(self.model, self.observation, preprocess, **kwargs)
        torch.testing.assert_close(actual, self.sample("baseline"), rtol=0, atol=0)

    def test_geometry_and_noise_contract_reject_bad_comparisons(self):
        for mode in ("baseline", "jacobian_identity", "upstream_approx"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.sample(mode, noise=self.noise[:, :-1])
                with self.assertRaises(ValueError):
                    self.sample(mode, noise=self.noise.double())
                with self.assertRaises(ValueError):
                    self.sample(mode, steps=0)
                with self.assertRaises(ValueError):
                    self.sample(mode, delay=20, shift=16)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CandidateTests))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "tests_run": result.testsRun, "failures": [(str(t), e) for t, e in result.failures],
            "errors": [(str(t), e) for t, e in result.errors], "success": result.wasSuccessful(),
            "torch_version": torch.__version__, "device": "cpu", "cuda_used": False,
            "upstream_commit": "44418454146c1d89e428d3d1b2c1ac5928258238",
        }, indent=2) + "\n")
    sys.exit(0 if result.wasSuccessful() else 1)
