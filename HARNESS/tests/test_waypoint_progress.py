"""Interior arrival does not waive endpoint settling, feedback or publication evidence."""
import json

import pytest

from f1_harness.contracts import Mode, SkillIntent
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.owner_bridge import JointProposal, OwnerBridge


@pytest.fixture
def path_bridge(tmp_path):
    now=[1_000_000_000]
    engine=TaskEngine(EvidenceStore(tmp_path),mode=Mode.FAKE,clock=lambda:now[0])
    engine.authorized=True
    engine.epoch=1
    engine.running=SkillIntent(request_id='path',session_id='session',owner_instance_id=engine.instance,
        task_id='task',task_hash='task-hash',epoch=engine.epoch,observation_id='observation',
        scene_version='scene',calibration_id='cal',skill='approach_grasp')
    bridge=OwnerBridge(engine,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=500_000_000,reference_tolerance_deg=.5,clock=lambda:now[0])
    bridge.plan_held_reference=(0.,)*16
    yield bridge,engine,now
    bridge.close()


def install(bridge,targets):
    bridge.proposal=JointProposal('path',bridge.engine.epoch,'scene','cal',(0.,)*22,
        tuple(tuple(row) for row in targets),.1,300_000_000,1.)
    bridge.current_target=targets[0]


def publish(bridge,now,offset_ms,measured,*,stamp_ms=None,complete=True):
    now[0]=1_000_000_000+offset_ms*1_000_000
    stamp=now[0] if stamp_ms is None else 1_000_000_000+stamp_ms*1_000_000
    bridge.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=(stamp,)*16)
    bridge.after_publication(publication_id=str(offset_ms),measured16=measured,
        publication_complete=complete,now_ns=now[0])


def test_interior_needs_post_publication_arrival_and_endpoint_settles(path_bridge):
    bridge,engine,now=path_bridge
    first=[0.]*16;first[7]=1.
    last=[0.]*16;last[7]=2.
    install(bridge,[first,last])
    publish(bridge,now,0,first)
    publish(bridge,now,20,first,stamp_ms=0)
    assert bridge.index==0  # Pre-publication sample cannot prove arrival.
    publish(bridge,now,40,[0.]*16)
    assert bridge.index==0  # A fresh but lagging arm cannot advance.
    publish(bridge,now,60,first)
    assert bridge.index==1 and bridge.current_target is None
    publish(bridge,now,80,last)
    assert bridge.index==1  # No next target was selected/published yet.
    bridge.current_target=last
    publish(bridge,now,100,last)
    publish(bridge,now,120,last)
    publish(bridge,now,400,last)
    assert bridge.index==1
    publish(bridge,now,420,last)
    assert bridge.index==2 and bridge.proposal is None
    rows=[json.loads(line) for line in (engine.store.root/'trace.jsonl').read_text().splitlines()]
    reached=[row for row in rows if row['kind']=='harness_measured_waypoint']
    assert [row['settle_required'] for row in reached]==[False,True]


def test_both_sides_of_gripper_change_keep_settling(path_bridge):
    bridge,_,now=path_bridge
    open_target=[0.]*16;closed=open_target.copy();closed[15]=60.
    after=closed.copy();after[7]=1.
    install(bridge,[open_target,closed,after])
    publish(bridge,now,0,open_target)
    publish(bridge,now,20,open_target)
    publish(bridge,now,300,open_target)
    assert bridge.index==0
    publish(bridge,now,320,open_target)
    assert bridge.index==1
    bridge.current_target=closed
    publish(bridge,now,340,open_target)
    publish(bridge,now,360,open_target)
    assert bridge.index==1
    publish(bridge,now,400,closed)
    publish(bridge,now,680,closed)
    assert bridge.index==1
    publish(bridge,now,700,closed)
    assert bridge.index==2


@pytest.mark.parametrize('fault',['cancel','old_epoch','incomplete','stale'])
def test_old_or_unconfirmed_path_never_advances(path_bridge,fault):
    bridge,engine,now=path_bridge
    target=[0.]*16
    install(bridge,[target,target])
    publish(bridge,now,0,target)
    if fault=='cancel':bridge.revoke('web_stop_requested')
    elif fault=='old_epoch':engine.epoch+=1
    publish(bridge,now,600 if fault=='stale' else 20,target,
        stamp_ms=0 if fault=='stale' else None,complete=fault!='incomplete')
    assert bridge.index==0
    assert bridge.proposal is None


def test_endpoint_error_restarts_stationary_window(path_bridge):
    bridge,_,now=path_bridge
    target=[0.]*16;moving=target.copy();moving[7]=.2
    install(bridge,[target])
    publish(bridge,now,0,target)
    publish(bridge,now,20,target)
    publish(bridge,now,200,moving)
    publish(bridge,now,220,target)
    publish(bridge,now,400,target)
    assert bridge.index==0
    publish(bridge,now,520,target)
    assert bridge.index==1
