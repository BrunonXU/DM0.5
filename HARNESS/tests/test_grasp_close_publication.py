"""Contact context is issued only after the owner actually publishes close."""
import numpy as np
import pytest

from f1_harness.contracts import Observation, SceneObject, Task
from test_waypoint_progress import path_bridge, install, publish


def setup_contact(bridge, engine):
    engine.task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    engine.running=engine.running.model_copy(update=dict(skill='grasp'))
    target=[0.]*16;target[15]=100
    install(bridge,[target])
    from dataclasses import replace
    bridge.proposal=replace(bridge.proposal,geometry_trace=(dict(semantic_stage='grasp_close',
        start_index=0,end_index=1,gripper=100.,goals=[np.eye(4).tolist()]),))
    bridge.plan_observation=Observation(observation_id='o',acquisition_instance='a',received_ns=1,
        clock_domain='host_monotonic',objects=(SceneObject(object_id='block',shape='square',
            frame_id='native',pose=tuple(np.eye(4).ravel())),))
    return target


def test_pending_close_is_not_a_physical_contact_context(path_bridge):
    bridge,engine,now=path_bridge;target=setup_contact(bridge,engine)
    assert 'grasp_contact' not in engine.status()['perception_context']
    bridge.current_target=list(target);bridge.current_target[15]=18
    publish(bridge,now,0,[0.]*16)
    assert engine.grasp_contact is None
    bridge.current_target=target
    publish(bridge,now,20,[0.]*16)
    value=engine.status()['perception_context']['grasp_contact']
    assert value['published_ns']==now[0] and value['target_closure']==100
    assert value['object_id']=='block'
    assert value['expected_eef_object']==list(np.eye(4).ravel())
    publish(bridge,now,40,[0.]*16)
    assert engine.grasp_contact==value  # original receipt time cannot refresh
    engine.revoke('test_end')
    assert engine.grasp_contact is None


def test_partial_publication_never_issues_grasp_contact(path_bridge):
    bridge,engine,now=path_bridge;setup_contact(bridge,engine)
    publish(bridge,now,0,[0.]*16,complete=False)
    assert engine.grasp_contact is None
