"""Human publication provenance, reviewed boundaries and RL terminal checks."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys import rlt_human_replay as module


def fixture():
    cfg = json.loads((TASK / "configs/rlt_b1250_stage2_offline.json").read_text())
    origin = 1_000_000_000
    observations, frames, controls = [], [], []
    for i in range(73):
        t = origin + round(i * 1e9 / 30)
        state = [0.0]*16
        state[15] = 100.0 if i < 30 else 0.0
        obs = dict(schema_version=3, episode_id="raw1", observation_id=i,
            available_monotonic_ns=t, capture_started_monotonic_ns=t-2_000_000,
            hil_state="hil_latched", intervention_id=1, policy_epoch=2,
            state16=state, state_timestamps_ns=[t-1_000_000]*16, camera_discontinuity=False)
        observations.append(obs)
        frames.append(dict(obs, frames=[dict(camera=c,host_received_monotonic_ns=t-3_000_000) for c in module.CAMERAS]))
    for i in range(601):
        t = origin+i*4_000_000
        opened = i >= 250
        grip_stamp = origin+(1_000_000_000 if opened else 0)-20
        action = [0.5]*16
        action[15] = 0.0 if opened else 100.0
        pubs = [dict(topic_key="joint_command",monotonic_ns=t-10,publisher_gid="arm-owner")]
        if i in (0,250):
            pubs.append(dict(topic_key="right_gripper_command",monotonic_ns=grip_stamp,publisher_gid="grip-owner"))
        controls.append(dict(schema_version=3,episode_id="raw1",monotonic_ns=t,
            started_monotonic_ns=t-100,completed_monotonic_ns=t,stream_sequence=i,command_sequence=i+1,
            hil_state="hil_latched",source="human_hand" if i==250 else "human_arm",
            policy_epoch=2,intervention_id=1,execution_mode="powered",publication_complete=True,stage="completed",
            submitted=action,measured=[999.0]*16,human_candidate=[777.0]*16,
            policy_proposal=[888.0]*16,publications=pubs,
            action_source_timestamps_ns=[t-10]*15+[grip_stamp]))
    start,end = origin+100_000_000,origin+1_800_000_000
    review = dict(human_review_accepted=True,task_success=True,source_group="one-session",split="train",
        accepted_interventions=[1],accepted_time_ranges_ns=[[start,origin+2_200_000_000]],
        environment_assistance_ranges_ns=[],phase_ranges=[
            dict(stage="alignment_insertion",start_ns=start,end_ns=origin+999_999_999),
            dict(stage="release",start_ns=origin+1_000_000_000,end_ns=origin+1_000_000_000),
            dict(stage="withdrawal",start_ns=origin+1_000_000_001,end_ns=origin+1_999_999_999)])
    segment=dict(id="human",source_run="raw1",source_group="one-session",split="train",start_ns=start,end_ns=end,
        outcome="success",terminal_evidence=dict(observation_id=54,available_monotonic_ns=end,
            success_after_retreat_confirmed=True,evidence_refs=["synthetic-endpoint-review"]))
    return dict(observations=observations,control=controls),frames,segment,cfg,review


class HumanReplayTests(unittest.TestCase):
    def test_local_objective_requires_release_review_and_cuts_actions(self):
        streams, frames, segment, cfg, review = fixture()
        cfg['objective'] = 'approach_place_release_v1'
        with self.assertRaisesRegex(ValueError, 'separately reviewed'):
            module.select_human_segment(streams, frames, segment, cfg, review)
        end = streams['observations'][33]['available_monotonic_ns']
        segment['end_ns'] = end
        segment['terminal_evidence'] = dict(observation_id=33, available_monotonic_ns=end,
            placement_release_confirmed=True, reviewer='synthetic-review', evidence_refs=['synthetic-frame'])
        records, report = module.select_human_segment(streams, frames, segment, cfg, review)
        self.assertEqual(report['success_rewards'], 1)
        self.assertTrue(all(r['next_decision_ns'] <= end for r in records))

    def test_real_targets_human_flags_and_one_success_terminal(self):
        data=fixture();before=copy.deepcopy(data)
        records,report=module.select_human_segment(*data)
        self.assertGreater(len(records),0)
        self.assertEqual(report["success_rewards"],1)
        self.assertEqual(report["terminal_transitions"],1)
        self.assertTrue(all(r["intervene_flags"]==[True]*16 for r in records))
        self.assertTrue(all(a[:14]==[0.5]*14 for r in records for a in r["submitted16"]))
        self.assertTrue(all(r["source_group"]=="one-session" for r in records))
        self.assertEqual(data,before)

    def test_persistent_gripper_requires_original_publication(self):
        streams,frames,segment,cfg,review=fixture()
        streams["control"][250]["publications"]=[streams["control"][250]["publications"][0]]
        records,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertIn("unverified_publication_target",report["rejected"])
        self.assertEqual(report["success_rewards"],0)
        self.assertFalse(any(r["terminated"] for r in records))

    def test_changing_held_target_without_publication_is_rejected(self):
        streams,frames,segment,cfg,review=fixture()
        streams["control"][251]["submitted"][15]=25.0
        _,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertIn("unverified_publication_target",report["rejected"])

    def test_unpublished_between_resampled_actions_is_rejected(self):
        streams,frames,segment,cfg,review=fixture()
        streams["control"][251]["publication_complete"]=False
        records,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertIn("unpublished_control",report["rejected"])
        t=streams["control"][251]["monotonic_ns"]
        self.assertTrue(all(not(r["decision_ns"]<=t<=r["next_decision_ns"]) for r in records))

    def test_policy_home_and_intervention_boundaries_are_not_expert_actions(self):
        for field,value,reason in [("source","home","non_human_or_home_control"),
                                   ("source","policy","non_human_or_home_control"),
                                   ("policy_epoch",3,"epoch_or_intervention_boundary"),
                                   ("intervention_id",2,"epoch_or_intervention_boundary")]:
            with self.subTest(field=field,value=value):
                streams,frames,segment,cfg,review=fixture()
                streams["control"][400][field]=value
                _,report=module.select_human_segment(streams,frames,segment,cfg,review)
                self.assertIn(reason,report["rejected"])
                self.assertEqual(report["success_rewards"],0)

    def test_home_event_is_not_terminal_evidence(self):
        streams,frames,segment,cfg,review=fixture()
        segment["terminal_evidence"]={"home_started":True}
        with self.assertRaisesRegex(ValueError,"inspected"):
            module.select_human_segment(streams,frames,segment,cfg,review)

    def test_bad_last_observation_does_not_create_fake_terminal(self):
        streams,frames,segment,cfg,review=fixture()
        frames[54]["frames"][0]["host_received_monotonic_ns"]=10**15
        records,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertTrue(records)
        self.assertEqual(report["success_rewards"],0)
        self.assertEqual(report["terminal_transitions"],0)

    def test_success_endpoint_must_be_the_reviewed_exact_observation(self):
        streams,frames,segment,cfg,review=fixture()
        segment["terminal_evidence"]["observation_id"]=53
        _,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertIn("terminal_observation_or_open_gripper_mismatch",report["rejected"])
        self.assertEqual(report["success_rewards"],0)

    def test_right_invalid_state_and_future_publications_rejected(self):
        streams,frames,segment,cfg,review=fixture()
        for row in streams["observations"]:
            row["state_valid_mask"]=[True]*16;row["state_valid_mask"][15]=False
        records,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertFalse(records)
        self.assertIn("right_state_invalid",report["rejected"])
        streams,frames,segment,cfg,review=fixture()
        streams["control"][400]["action_source_timestamps_ns"][15]=10**15
        _,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertIn("noncausal_publication",report["rejected"])

    def test_split_review_and_environment_boundaries_rejected(self):
        for change in ("split","assistance","home"):
            streams,frames,segment,cfg,review=fixture()
            if change=="split":segment["split"]="validation"
            elif change=="assistance":review["environment_assistance_ranges_ns"]=[[segment["start_ns"]+1,segment["end_ns"]-1]]
            else:segment["end_ns"]=3_000_000_000
            with self.subTest(change=change),self.assertRaises(ValueError):
                module.select_human_segment(streams,frames,segment,cfg,review)

    def test_truncated_prefix_does_not_receive_success_or_true_terminal(self):
        streams,frames,segment,cfg,review=fixture();segment["outcome"]="truncated"
        records,report=module.select_human_segment(streams,frames,segment,cfg,review)
        self.assertEqual(report["success_rewards"],0)
        self.assertEqual(report["terminal_transitions"],0)
        self.assertEqual(report["truncated_transitions"],1)

    def test_actual_raw_hash_and_review_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"raw1"; (root/"journal").mkdir(parents=True);(root/"payload").mkdir()
            streams,frames,_,_,_=fixture()
            streams.update(events=[],policy_chunks=[])
            for name,records in streams.items():
                (root/"journal"/(name+".jsonl")).write_text("".join(json.dumps(r)+"\n" for r in records))
            (root/"payload/frames.jsonl").write_text("".join(json.dumps(r)+"\n" for r in frames))
            (root/"payload/head.pack").write_bytes(b"synthetic payload")
            payload=dict(complete=True,drop_count=0,writer_error=None,count=len(frames),pack_files={"head":{
                "path":"head.pack","bytes":17,"sha256":module.digest(root/"payload/head.pack")}})
            (root/"payload/manifest.json").write_text(json.dumps(payload))
            manifest=dict(schema_version=3,episode_id="raw1",contract="f1_upper_16d_v1",complete=True,
                drop_count=0,writer_error=None,training_ready=False,action_contract={"dimension":16},
                binding=dict(execution_mode="powered",rtc_hil=True,model_id="historical-Full1K",runtime_identity={}),
                counts={k:len(v) for k,v in streams.items()},
                stream_sha256={k:module.digest(root/"journal"/(k+".jsonl")) for k in streams},
                payload_sha256={k:module.digest(root/"payload"/k) for k in ("manifest.json","frames.jsonl")})
            (root/"journal/manifest.json").write_text(json.dumps(manifest))
            review=dict(manifest_sha256=module.digest(root/"journal/manifest.json"),contract="f1_upper_16d_v1",
                human_review_accepted=True,accepted_interventions=[1],accepted_time_ranges_ns=[[1,2]],source_group="group")
            path=Path(tmp)/"review.json";path.write_text(json.dumps(review))
            _,_,receipt=module.audit_human_raw(root,path)
            self.assertEqual(receipt["model_id"],"historical-Full1K")
            (root/"payload/head.pack").write_bytes(b"changed contents!")
            with self.assertRaisesRegex(ValueError,"pack digest"):
                module.audit_human_raw(root,path)
            review["manifest_sha256"]="wrong";path.write_text(json.dumps(review))
            with self.assertRaisesRegex(ValueError,"bound"):
                module.audit_human_raw(root,path)


if __name__=="__main__":
    unittest.main()
