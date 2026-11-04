"""Pure-data identity for supervised synchronous and RTC RLT runtimes.

The historical BC export remains immutable. This separate runtime contract
describes how its C16 output is proposed, without claiming robot acceptance.
It is also used by the offline deployment assembler.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

RLT_PROTOCOL = "f1_eef_rlt_sync_v1"
RLT_RTC_PROTOCOL = "f1_eef_rlt_rtc_v1"
RLT_RTC_MODE = 'rtc_c16_committed_prefix_v1'
RLT_CONTROL_MODE = "rlt_hil_supervised"
RLT_TIME_CONTRACT = dict(prediction_horizon=16, reference_horizon=50,
    execute_steps=16, chunk_steps=16, frequency_hz=30,
    execution_mode="sync_c16_hold_then_execute", control_mode=RLT_CONTROL_MODE,
    reference_mode="fresh_frozen_B_unguided_5step", deterministic_actor=True,
    rtc=False, shift_actor=False, append_reference_tail=False)


def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            value.update(block)
    return value.hexdigest()


def load_rlt_identity(head_root, encoder_root, base_model_version, *, allow_online=False, rtc=False):
    """Verify immutable assets and return the deployment's composite identity."""
    head_root, encoder_root = Path(head_root), Path(encoder_root)
    head_path, encoder_path = head_root/"manifest.json", encoder_root/"manifest.json"
    head, encoder = json.loads(head_path.read_text()), json.loads(encoder_path.read_text())
    online = head.get('schema') == 'f1_rlt_local_head_v2'
    if (head.get("schema") != "f1_rlt_stage2_head_v1" and not (allow_online and online)) or encoder.get("schema") != "f1_rlt_encoder_v1":
        raise ValueError("Unsupported RLT artifact schema")
    if head.get("parent_bundle_sha256") != base_model_version or encoder.get("parent_bundle_sha256") != base_model_version:
        raise ValueError("RLT assets belong to a different base model")
    if head.get("encoder_manifest_sha256") != file_sha(encoder_path):
        raise ValueError("RLT head belongs to a different encoder")
    if online:
        if (head.get('objective') != 'approach_place_release_v1'
                or head.get('weights_scope') != 'actor_only'
                or head.get('phase') not in ('bc', 'q')
                or type(head.get('q_guidance_updates')) is not int
                or head['q_guidance_updates'] < 0
                or (head['phase'] == 'bc' and head['q_guidance_updates'] != 0)):
            raise ValueError('Invalid online local actor contract')
    elif head.get("phase") != "bc" or head.get("q_guidance_updates") != 0:
        raise ValueError("This supervised runtime accepts an explicit BC-only candidate")
    expected = dict(z_dim=2048, proprio_dim=8, action_dim=10, chunk_len=16, ref_chunk_len=50)
    if any(head.get("model_config", {}).get(key) != value for key, value in expected.items()):
        raise ValueError("RLT runtime requires the trained C16 / H50 / right EEF10 contract")
    if head.get("output_parameterization") != "linear_normalized_xyz_tanh_rotation6d_gripper":
        raise ValueError("RLT output parameterization changed")
    if head.get("head_sha256") != file_sha(head_root/"head.safetensors"):
        raise ValueError("RLT head weights changed")
    if encoder.get("encoder_sha256") != file_sha(encoder_root/"encoder.safetensors"):
        raise ValueError("RLT encoder weights changed")
    from .rlt_sampling import sampling_contract
    sampling = sampling_contract(head)
    timing = dict(RLT_TIME_CONTRACT)
    if rtc:
        timing.update(execution_mode=RLT_RTC_MODE, rtc=True,
                      reference_mode='fresh_frozen_B_with_known_committed_prefix',
                      prefix_contract='known_prefix_detached_v1', decision_horizon='explicit_next_decision_steps')
    if sampling is not None:
        timing['deterministic_actor'] = sampling['mode'] == 'deterministic'
        timing['actor_sampling'] = sampling
    binding = dict(protocol=RLT_RTC_PROTOCOL if rtc else RLT_PROTOCOL, time_contract=timing,
        base_model_version=base_model_version, actor_manifest_sha256=file_sha(head_path),
        encoder_manifest_sha256=file_sha(encoder_path))
    version = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata = dict(base_model_version=base_model_version,
        encoder_manifest_sha256=binding["encoder_manifest_sha256"],
        actor_manifest_sha256=binding["actor_manifest_sha256"], actor_version=head["head_sha256"],
        checkpoint_step=head["checkpoint_step"], training_phase=head['phase'], q_guidance_updates=head['q_guidance_updates'],
        execution_mode=timing['execution_mode'], control_mode=RLT_CONTROL_MODE)
    if sampling is not None:
        metadata['actor_sampling'] = sampling
        metadata['deployment_scope'] = head.get('deployment_scope')
    return dict(model_id=('f1-toys-rlt-rtc-' if rtc else 'f1-toys-rlt-sync-')+version[:16], model_version=version, rlt=metadata,
        runtime_manifest=dict(schema="f1_rlt_supervised_rtc_runtime_v1" if rtc else "f1_rlt_supervised_sync_runtime_v1", binding=binding,
            source_export_deployment_ready=head.get("deployment_ready"),
            source_export_reference_mode=head.get("reference_mode"),
            reference_compute_path="training_eager_prefix_and_suffix",
            critic_used_for_control=False, original_export_modified=False,
            rtc_supported=rtc, robot_acceptance="not_established_by_model_loading"))
