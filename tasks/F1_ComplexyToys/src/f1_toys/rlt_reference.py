"""Offline RLT reference extraction around the unchanged F1 RTC sampler.

This is a feature adapter, not an online environment or actuator. A reference
plan is never marked as an executed RL transition. The owning Policy must not
be shared outside this adapter while a request is in flight.
"""
from __future__ import annotations

import threading

import numpy as np
import torch


class RLTReferenceAdapter:
    def __init__(self, reader):
        self.reader = reader
        self.policy = reader.policy
        self.lock = threading.Lock()

    def predict_rtc(self, images, state16, held16, rtc, **kwargs):
        with self.lock:
            model = self.policy.model
            original = model.build_prefix_cache
            had_instance_override = "build_prefix_cache" in model.__dict__
            captured = []

            def capture(observation):
                prefix, mask, kv = original(observation)
                captured.append((prefix.detach(), mask.detach(), observation.state.detach()))
                return prefix, mask, kv

            model.build_prefix_cache = capture
            try:
                decoded, raw, data = self.policy.predict_rtc(images, state16, held16, rtc, **kwargs)
            finally:
                if had_instance_override:
                    model.build_prefix_cache = original
                else:
                    del model.build_prefix_cache
            if len(captured) != 1:
                raise ValueError("RLT reference requires exactly one matching prefix forward")
            prefix, mask, state = captured[0]
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16,
                    enabled=str(self.policy.device).startswith("cuda")):
                z = self.reader.encoder(prefix, mask).flatten(1).float()
            raw = np.asarray(raw)
            if raw.shape != (50,32) or not np.isfinite(raw).all() or np.any(raw[:,10:] != 0):
                raise ValueError("Reference violates F1 H50 / active EEF10 contract")
            features = {"z_rl": z,
                "proprio": state[:, self.policy.contract["active_state_indices"]].float(),
                "ref_chunk": torch.as_tensor(raw[None,:,:10], device=z.device, dtype=torch.float32),
                "action_space": "f1_normalized_relative_eef10_v1",
                "anchor_state16": np.asarray(state16, dtype=float).copy(),
                "committed_prefix_steps": len(rtc["committed_actions16"]),
                "prefix_forward_calls": len(captured),
                "executed_transition": False}
            return decoded, raw, data, features
