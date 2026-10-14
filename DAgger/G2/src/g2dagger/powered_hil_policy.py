"""Synchronous policy chunks with continuous shared acquisition for Powered HIL.

No hardware commands here. The caller owns the sole 100 Hz dispatch boundary.
"""
from dataclasses import replace
import time
import numpy as np

from .hil_real_shadow import RealObservationHILShadow
from .hil_supervisor import HILState
from .real_observation_source import frames_advance, policy_input_issue


from dagger_core.powered_hil_policy import ChunkPolicy

class PoweredHILPolicy(ChunkPolicy):
    def __init__(self, emit, source, infer, recorder, sink, *, require_start=True,
                 action_dim=34, chunk_steps=50, waypoint_hz=30.,
                 input_validator=policy_input_issue, frame_advancer=frames_advance, **kwargs):
        super().__init__(emit, source, infer, recorder, sink, require_start=require_start,
            action_dim=action_dim, chunk_steps=chunk_steps, waypoint_hz=waypoint_hz,
            input_validator=input_validator, frame_advancer=frame_advancer, **kwargs)

    def record_chunk(self, obs, epoch, request_id, started, response, actions):
        """Robot-specific serialization; the default retains the G2 journal."""
        if not self.recorder.record_policy_chunk(policy_epoch=epoch, request_id=request_id,
                observation_request_id=obs.observation_id,
                observation_monotonic_ns=obs.capture_started_monotonic_ns,
                request_monotonic_ns=started, response_monotonic_ns=response,
                actions_50x34=actions):
            raise RuntimeError("policy journal rejected record")

    def sampling_metadata(self, lower, upper, alpha):
        return dict(kind='linear_anchor_30hz', source_hz=self.waypoint_hz,
            chunk_started_monotonic_ns=self.chunk_started_ns,
            lower_index=lower, upper_index=upper, alpha=alpha,
            anchor_34d=self.chunk_anchor.tolist() if lower == -1 else None,
            raw_waypoint_34d=self.chunk[self.action_index].tolist())

