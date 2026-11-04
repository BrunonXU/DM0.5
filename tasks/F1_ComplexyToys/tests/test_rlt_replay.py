"""Published-action, schema4 validity and episode-boundary regression checks."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

TASK=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("rlt_replay",TASK/"src/f1_toys/rlt_replay.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    cfg=json.loads((TASK/"configs/rlt_b1250_stage2_offline.json").read_text())
    origin=1_000_000_000
    mask=[True]*16;mask[14]=False
    observations=[];payload=[];controls=[]
    for i in range(61):
        t=origin+round(i*1e9/30)
        stamps=[t-1_000_000]*16;stamps[14]=0
        obs=dict(observation_id=i,available_monotonic_ns=t,capture_started_monotonic_ns=t-2_000_000,
            hil_state="policy",state16=[0.0]*16,state_timestamps_ns=stamps,state_valid_mask=mask,
            camera_discontinuity=False,policy_epoch=0,intervention_id=0)
        observations.append(obs)
        payload.append(dict(obs,frames=[dict(camera=c,host_received_monotonic_ns=t-3_000_000) for c in module.CAMERAS]))
    for i in range(501):
        t=origin+i*4_000_000
        stamps=[t-100]*16;stamps[14]=None
        controls.append(dict(monotonic_ns=t,stream_sequence=i,command_sequence=i+1,
            hil_state="policy",source="policy",policy_epoch=0,intervention_id=0,
            execution_mode="powered",publication_complete=True,stage="completed",
            submitted=[0.5]*16,policy_proposal=[999.0]*16,action_valid_mask=mask,
            action_source_timestamps_ns=stamps))
    segment=dict(id="one",source_run="raw1",split="train",start_s=0.1,end_s=1.8,outcome="success")
    return {"observations":observations,"control":controls},payload,segment,cfg


class ReplayTests(unittest.TestCase):
    def test_schema4_missing_left_is_not_missing_right(self):
        records,report=module.select_segment(*fixture())
        self.assertGreater(len(records),0)
        self.assertEqual(report["success_rewards"],1)
        self.assertEqual(sum(r["terminated"] for r in records),1)

    def test_proposals_are_never_executed_labels(self):
        records,_=module.select_segment(*fixture())
        self.assertTrue(all(a==[0.5]*16 for r in records for a in r["submitted16"]))

    def test_truncation_does_not_become_failure_terminal(self):
        streams,payload,segment,cfg=fixture();segment["outcome"]="truncated"
        records,report=module.select_segment(streams,payload,segment,cfg)
        self.assertEqual(report["success_rewards"],0)
        self.assertFalse(any(r["terminated"] for r in records))
        self.assertEqual(sum(r["truncated"] for r in records),1)

    def test_failure_terminal_has_no_success_reward(self):
        streams,payload,segment,cfg=fixture();segment["outcome"]="failure"
        records,report=module.select_segment(streams,payload,segment,cfg)
        self.assertEqual(report["success_rewards"],0)
        self.assertEqual(sum(r["terminated"] for r in records),1)

    def test_unpublished_control_inside_window_is_rejected(self):
        streams,payload,segment,cfg=fixture()
        streams["control"][250]["publication_complete"]=False
        records,report=module.select_segment(streams,payload,segment,cfg)
        self.assertIn("unpublished_control",report["rejected"])
        t=streams["control"][250]["monotonic_ns"]
        self.assertTrue(all(not(r["decision_ns"]<=t<=r["next_decision_ns"]) for r in records))

    def test_future_images_rejected(self):
        streams,payload,segment,cfg=fixture()
        for row in payload:
            row["frames"][0]["host_received_monotonic_ns"]=10**15
        records,report=module.select_segment(streams,payload,segment,cfg)
        self.assertFalse(records)
        self.assertIn("camera_stale_or_future",report["rejected"])

    def test_right_state_invalid_rejected(self):
        streams,payload,segment,cfg=fixture()
        for row in streams["observations"]:
            row["state_valid_mask"]=list(row["state_valid_mask"])
            row["state_valid_mask"][15]=False
        records,report=module.select_segment(streams,payload,segment,cfg)
        self.assertFalse(records)
        self.assertIn("right_state_missing",report["rejected"])

    def test_epoch_and_recovery_boundaries_rejected(self):
        streams,payload,segment,cfg=fixture()
        streams["control"][250]["hil_state"]="safe_hold"
        _,report=module.select_segment(streams,payload,segment,cfg)
        self.assertIn("non_policy_or_recovery_control",report["rejected"])

    def test_segments_cannot_cross_reset_exclusion(self):
        streams,payload,segment,cfg=fixture()
        segment.update(start_s=1.0,end_s=1.9)
        records,_=module.select_segment(streams,payload,segment,cfg)
        self.assertTrue(records)
        self.assertTrue(all(r["decision_ns"]>=2_000_000_000 and r["next_decision_ns"]<=2_900_000_000 for r in records))

    def test_raw_rows_are_not_mutated(self):
        data=fixture();before=copy.deepcopy(data)
        module.select_segment(*data)
        self.assertEqual(data,before)


if __name__=="__main__":
    unittest.main()
