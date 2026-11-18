"""Deterministic, explicitly synthetic task plant for integration and fault injection."""
from __future__ import annotations

import uuid

from .contracts import HarnessError, Mode, Observation, SceneObject, SkillResult
from .verification import verify


class FakePlant:
    def __init__(self, engine, failures: dict[str, int] | None = None):
        if engine.mode != Mode.FAKE:
            raise ValueError("FakePlant requires explicit fake mode")
        self.engine = engine
        self.failures = dict(failures or {})
        self.held = self.inserted = self.aligned = False
        self.approached = self.transferred = self.inspected = self.reset = False
        self.steps = 0
        self.evidence = "synthetic-initial"

    def observe(self):
        self.steps += 1
        task = self.engine.task
        observation = Observation(observation_id="fake:" + uuid.uuid4().hex,
            acquisition_instance="fake", received_ns=self.engine.clock(), clock_domain="fake",
            calibration_id=task.calibration_id if task else "synthetic-calibration",
            scene_version=f"synthetic:{self.steps}", stationary=True, synthetic=True,
            objects=(SceneObject(object_id=task.object_id if task else "block-1", shape="square",
                pose=(1.,0.,0.,.25,0.,1.,0.,.1,0.,0.,1.,.05,0.,0.,0.,1.),
                frame_id="synthetic-base", uncertainty_m=.001,
                eef_object_pose=(1.,0.,0.,.01,0.,1.,0.,0.,0.,0.,1.,.04,0.,0.,0.,1.) if self.held else None,
                held=self.held, inserted=self.inserted, stationary=True,
                inserted_hole_id=task.hole_id if task and self.inserted else None,
                evidence_ids=(self.evidence,)),
                SceneObject(object_id=task.hole_id if task else "hole-1", shape="square",
                    pose=(1.,0.,0.,.35,0.,1.,0.,-.1,0.,0.,1.,.1,0.,0.,0.,1.),
                    frame_id="synthetic-base", uncertainty_m=.001, stationary=True,
                    evidence_ids=(self.evidence,))))
        self.engine.update_observation(observation)
        return observation

    def advance(self):
        intent = self.engine.take_pending()
        if intent is None:
            return
        skill = intent.skill
        self.evidence = self.engine.store.event("synthetic_plant_action", skill=skill, request_id=intent.request_id)
        failure = self.failures.get(skill, 0) > 0
        if failure:
            self.failures[skill] -= 1
            if skill == "grasp":
                self.held = False
            result = SkillResult(request_id=intent.request_id, lifecycle="completed", outcome="failure",
                                 reason="injected_" + skill, evidence_ids=(self.evidence,), synthetic=True)
        else:
            error = None
            if skill == "approach_grasp":
                self.approached = True
            elif skill in ("grasp", "regrasp"):
                if not self.approached:
                    error = "approach_required"
                else:
                    self.held, self.inserted = True, False
            elif skill == "inspect_in_hand":
                self.inspected = self.held
            elif skill == "transfer_above_hole":
                if not self.held:
                    error = "verified_grasp_required"
                else:
                    self.transferred = True
            elif skill == "align":
                if not self.held or not self.transferred or not self.inspected:
                    error = "held_above_hole_required"
                else:
                    self.aligned = True
            elif skill == "insert":
                if not self.aligned or not self.held:
                    error = "alignment_required"
                else:
                    self.inserted = True
            elif skill == "release":
                if not self.inserted:
                    error = "insertion_evidence_required_before_release"
                else:
                    self.held = False
            elif skill == "withdraw_reset":
                if self.held or not self.inserted:
                    error = "verified_empty_tool_after_placement_required"
                else:
                    self.reset = True
            elif skill == "retreat":
                self.aligned, self.inserted = False, False
            observation = self.observe()
            if skill in ("verify_grasp", "inspect_in_hand", "verify_placed"):
                result = verify(skill, self.engine.task, observation, intent.request_id)
            else:
                result = SkillResult(request_id=intent.request_id, lifecycle="completed",
                                     outcome="failure" if error else "success", reason=error or "synthetic_action",
                                     evidence_ids=(self.evidence,), synthetic=True)
        self.engine.complete(intent.request_id, result)

    def start(self):
        self.observe()
        return self.engine.local_start(physical_event=True, feedback_healthy=True)


class ReplayObservations:
    """Only recorded observations, no synthetic reconstruction of missing depth."""
    def __init__(self, path):
        import json
        self.rows = [Observation.model_validate(json.loads(line)) for line in path.read_text().splitlines() if line]
        self.index = 0

    def observe(self):
        if self.index >= len(self.rows):
            raise HarnessError("replay_exhausted")
        value = self.rows[self.index]
        self.index += 1
        return value
