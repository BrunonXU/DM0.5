import asyncio
import copy
from io import BytesIO
import json
import struct
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from f1_toys.state_input_profile import (
    MASK, PROFILE, SEMANTICS, ModelStateCompletion, install_state_input_profile,
)


class CapturingProcessor:
    def observation(self, images, state, **kwargs):
        self.received = (images, state.copy(), kwargs)
        state[0] = -999  # Even a downstream mutation must not affect physical state.
        return {"state": state, "images": images}

    def model_state(self, state):
        return state


@pytest.fixture
def evidence(tmp_path):
    contract = dict(schema="f1_toys_eef_contract_v2", state_dim=16,
                    cameras=["head", "left_wrist", "right_wrist"])
    stats = dict(state=dict(low=[0.0] * 16, high=[100.0] * 16))
    policy = SimpleNamespace(contract=contract, stats=stats, processor=CapturingProcessor(),
                             bundle_id="test-legacy")
    dataset, bundle = tmp_path / "dataset", tmp_path / "bundle"
    dataset.mkdir(); bundle.mkdir()
    (dataset / "manifest.json").write_text('{"bound_train":true}\n')
    (bundle / "dataset_manifest.json").write_bytes((dataset / "manifest.json").read_bytes())
    (dataset / "contract.json").write_text(json.dumps(contract))
    (dataset / "norm_stats.json").write_text(json.dumps(stats))
    states = np.ones((4, 16)); states[:, 14] = 0
    np.savez(dataset / "arrays.npz", states=states)
    return policy, dataset, bundle


def metadata():
    return dict(state16=[1.0] * 14 + [0.0, 45.0],
                last_submitted16=[2.0] * 14 + [0.0, 46.0],
                state_valid_mask=MASK.copy(), absent_state_semantics=SEMANTICS,
                state_timestamps_ns=[101] * 14 + [0, 102])


def test_bound_training_constant_installs_model_copy_only(evidence):
    policy, dataset, bundle = evidence
    original = policy.processor
    profile = install_state_input_profile(PROFILE, policy, dataset, bundle)
    assert policy.processor.original is original
    assert profile.identity["training_evidence"]["anchors"] == 4
    assert profile.identity["physical_feedback_modified"] is False
    raw = np.arange(16, dtype=np.float32)
    before = raw.copy(); images = {"three": "unchanged"}
    policy.processor.observation(images, raw, prepared_pixels=True)
    np.testing.assert_array_equal(raw, before)
    expected = before.copy(); expected[14] = 0
    np.testing.assert_array_equal(original.received[1], expected)
    assert original.received[0] is images
    assert original.received[2] == {"prepared_pixels": True}
    assert not np.shares_memory(policy.processor.model_state(raw), raw)


def test_request_keeps_all_physical_metadata_unchanged(evidence):
    profile = install_state_input_profile(PROFILE, *evidence)
    meta = metadata(); before = copy.deepcopy(meta)
    assert profile.validate_request(meta)["name"] == PROFILE
    assert meta == before


@pytest.mark.parametrize("change", [
    {"state_valid_mask": [True] * 16},
    {"state_valid_mask": [False] * 16},
    {"state_valid_mask": [True] * 13 + [False, False, True]},
    {"state_valid_mask": [1] * 14 + [0, 1]},
    {"absent_state_semantics": "measured"},
    {"state16": [1.0] * 16},
    {"last_submitted16": [1.0] * 16},
    {"state_timestamps_ns": [101] * 16},  # Cached zero is not a fresh receipt.
    {"state_timestamps_ns": [0] * 16},
    {"state_timestamps_ns": [101] * 14 + [0, 0]},
])
def test_wrong_missingness_or_fake_measurement_rejected(evidence, change):
    profile = install_state_input_profile(PROFILE, *evidence)
    with pytest.raises(ValueError):
        profile.validate_request(dict(metadata(), **change))


@pytest.mark.parametrize("change", ["manifest", "contract", "statistics", "nonconstant", "rightonly"])
def test_wrong_evidence_cannot_enable_profile(evidence, change):
    policy, dataset, bundle = evidence
    if change == "manifest":
        (dataset / "manifest.json").write_text("{}")
    elif change == "contract":
        (dataset / "contract.json").write_text("{}")
    elif change == "statistics":
        (dataset / "norm_stats.json").write_text("{}")
    elif change == "nonconstant":
        np.savez(dataset / "arrays.npz", states=np.ones((4, 16)))
    else:
        policy.contract["input_version"] = "right_only_v1"
    original = policy.processor
    with pytest.raises(ValueError):
        install_state_input_profile(PROFILE, policy, dataset, bundle)
    assert policy.processor is original


def test_disabled_profile_has_no_effect_or_evidence_reads(evidence):
    policy, _, _ = evidence; original = policy.processor
    assert install_state_input_profile(None, policy, "/missing", "/missing") is None
    assert policy.processor is original
    with pytest.raises(ValueError, match="Unsupported"):
        install_state_input_profile("unknown", policy, "/missing", "/missing")


def request_payload(policy, extra):
    meta = dict(contract="f1_upper_16d_v1", protocol="f1_eef_sync_v2", session_id="test",
                request_id=1, observation_id=1, policy_epoch=0, observation_monotonic_ns=105,
                state16=[0.0] * 16, last_submitted16=[0.0] * 16, frequency_hz=30,
                chunk_steps=20, execute_steps=20, prediction_horizon=50,
                model_id="f1-toys-eef-" + policy.bundle_id[:16], model_version=policy.bundle_id)
    meta.update(extra); frames = []; payloads = []
    for camera, size in [("head", (1280, 720)), ("left_wrist", (640, 480)), ("right_wrist", (640, 480))]:
        stream = BytesIO(); Image.new("RGB", size).save(stream, format="JPEG")
        data = stream.getvalue(); payloads.append(data)
        frames.append(dict(camera=camera, length=len(data), encoding="image/jpeg", width=size[0], height=size[1]))
    meta["frames"] = frames
    header = json.dumps(meta).encode()
    return struct.pack("!I", len(header)) + header + b"".join(payloads)


def test_server_profile_health_telemetry_and_rejection(evidence, tmp_path):
    pytest.importorskip("torch"); pytest.importorskip("aiohttp")
    from f1_toys.server import Server
    policy, _, _ = evidence
    profile = install_state_input_profile(PROFILE, *evidence)
    server = Server(policy, log=tmp_path / "requests.jsonl", state_input_profile=profile)
    health = json.loads(asyncio.run(server.health(None)).text)
    assert health["state_input_profile"] == profile.identity
    assert health["capabilities"]["cameras"] == ["head", "left_wrist", "right_wrist"]
    payload = request_payload(policy, metadata())
    result = server.compute(payload, echo=True)
    assert result["state_input_profile"]["physical_feedback_modified"] is False
    record = json.loads(server.log.read_text().splitlines()[0])
    assert record["state_input_profile"] == profile.identity
    bad = request_payload(policy, dict(metadata(), state_timestamps_ns=[100] * 16))
    with pytest.raises(ValueError, match="timestamp"):
        server.compute(bad, echo=True)
    server.record_error(bad, "invalid missing-hardware evidence")
    assert json.loads(server.log.read_text().splitlines()[-1])["state_input_profile"] == profile.identity


def test_disabled_server_preserves_existing_wire_shape(evidence):
    pytest.importorskip("torch"); pytest.importorskip("aiohttp")
    from f1_toys.server import Server
    policy, _, _ = evidence
    server = Server(policy)
    assert "state_input_profile" not in json.loads(asyncio.run(server.health(None)).text)
    assert "state_input_profile" not in server.compute(request_payload(policy, {}), echo=True)


@pytest.mark.parametrize("rtc", [False, True])
def test_compute_preserves_raw_fk_inputs_and_records_imputation(evidence, tmp_path, rtc):
    pytest.importorskip("torch"); pytest.importorskip("aiohttp")
    from f1_toys.server import Server
    policy, _, _ = evidence
    profile = install_state_input_profile(PROFILE, *evidence)
    seen = []

    def predict(images, state, held, *args, **kwargs):
        state_before, held_before = copy.deepcopy(state), copy.deepcopy(held)
        policy.processor.observation(images, state)
        assert state == state_before and held == held_before
        seen.append((state, held))
        return dict(success=True, actions=[held] * 20, timing_ms={}), np.zeros((50, 32)), None

    policy.predict_rtc = policy.predict_eef = predict
    server = Server(policy, rtc=rtc, log=tmp_path / "requests.jsonl", state_input_profile=profile)
    extra = metadata()
    if rtc:
        extra.update(protocol="f1_eef_rtc_v2", rtc=dict(bootstrap=True, delay_steps=14,
            shift_steps=16, coverage_steps=32, plan_origin_ns=0, base_plan_id=0,
            committed_actions16=[], previous_absolute_eef10=[]))
    result = server.compute(request_payload(policy, extra), echo=False)
    assert result["success"] is True and len(seen) == 1
    record = json.loads(server.log.read_text())
    replay = record["prediction_replay"]
    for field in ("state16", "last_submitted16", "state_timestamps_ns", "state_valid_mask", "absent_state_semantics"):
        assert replay[field] == extra[field]
    assert replay["state_input_profile"] == profile.identity
    with pytest.raises(ValueError):
        server.compute(request_payload(policy, dict(extra, state_valid_mask=[True] * 16)), echo=False)
    assert len(seen) == 1
