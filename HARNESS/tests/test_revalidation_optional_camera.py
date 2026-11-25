import pytest

from f1_harness.contracts import HarnessError, digest
from f1_harness.owner_bridge import JointProposal
from f1_harness.revalidation import RevalidationEvidenceUpdated, _measurement_identity, proof_observation
from test_plan_revalidation import observation, scenario


def without_head(obs):
    return obs.model_copy(update={
        "frames": tuple(frame for frame in obs.frames if frame.camera != "head"),
        "pairings": tuple(pair for pair in obs.pairings if pair.camera != "head"),
    })


def test_head_is_optional_for_measurement_identity_when_right_wrist_supplies_geometry():
    original = observation(1_000_000_000)
    assert _measurement_identity(original, without_head(original)) is True
    assert _measurement_identity(without_head(original), original) is True


@pytest.mark.parametrize("add_stream", [False, True])
def test_optional_change_requires_new_sweep_before_publication(scenario, add_stream):
    service, args, clock, original, latest, intent = scenario
    depth = next(f for f in latest.frames if f.camera == "right" and f.stream == "depth")
    extra = depth.model_copy(update={"camera": "left", "frame_id": "left_depth"})
    expanded = latest.model_copy(update={"frames": (*latest.frames, extra)})
    before, after = (latest, expanded) if add_stream else (expanded, latest)
    proof = service.dispatch("revalidate_plan", {**args, "observation": before.model_dump()})
    assert _measurement_identity(before, after, task=service.task, skill=intent.skill) is True
    scope = dict(proposal=JointProposal(**args["proposal"]), original=original,
                 intent=intent, task=service.task, now_ns=clock[0], max_age_ns=500_000_000)
    with pytest.raises(RevalidationEvidenceUpdated):
        proof_observation(proof, **scope, live_observation=after)
    rescanned = service.dispatch("revalidate_plan", {**args, "observation": after.model_dump()})
    assert rescanned["geometry_checks"]["swept_observation_sha256"] == digest(after)
    assert proof_observation(rescanned, **scope, live_observation=after) == after


def test_changed_shared_publisher_remains_an_identity_fault():
    original = observation(1_000_000_000)
    frames = tuple(frame.model_copy(update={"publisher_id": "different-source"})
                   if frame.camera == "head" else frame for frame in original.frames)
    with pytest.raises(HarnessError, match="revalidation_camera_identity_changed"):
        _measurement_identity(original, original.model_copy(update={"frames": frames}))


@pytest.mark.parametrize("missing_original", [False, True])
def test_geometry_camera_must_have_depth_on_both_sides(missing_original):
    original = observation(1_000_000_000)
    frames = tuple(f for f in original.frames if (f.camera, f.stream) != ("right", "depth"))
    missing = original.model_copy(update={"frames": frames})
    before, after = (missing, original) if missing_original else (original, missing)
    with pytest.raises(HarnessError, match="revalidation_camera_identity_changed"):
        _measurement_identity(before, after)


def test_precision_observation_without_single_camera_selector_keeps_both_pairs():
    original = observation(1_000_000_000).model_copy(update={"geometry_camera": None})
    assert not _measurement_identity(original, original)
    with pytest.raises(HarnessError, match="revalidation_camera_identity_changed"):
        _measurement_identity(original, without_head(original))
