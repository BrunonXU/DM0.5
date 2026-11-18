from copy import deepcopy
import json
import numpy as np
from f1_harness.planner_geometry import pack_geometry,unpack_geometry
from test_grasp_selection_planning import setup


def test_shared_obstacle_frames_are_lossless_and_do_not_modify_original():
    pose=np.eye(4).tolist();pose[0][3]=.12345678901234567
    original=dict(obstacles=[dict(pose=deepcopy(pose),min=[i,0,0],max=[i+1,1,1],role='bucket')
                             for i in range(12)]+[dict(type='halfspace',point=[0,0,0],normal=[0,0,1])])
    before=deepcopy(original);packed=pack_geometry(original)
    assert unpack_geometry(packed)==original==before
    assert len(packed['obstacle_pose_frames'])==1
    assert len(json.dumps(packed))<len(json.dumps(original))
    assert packed['obstacles'][-1]==original['obstacles'][-1]


def test_partial_unknown_obstacles_never_become_empty_space():
    original=dict(obstacles=None,object_pose=None,missing_entities=[{'object_id':'block'}])
    assert unpack_geometry(pack_geometry(original))==original


def test_object_relative_current_tcp_reconstructs_actual_world_tcp():
    service,obs,_,_=setup()
    g=service.dispatch('geometry_context',dict(observation=obs.model_dump(),full22=[0.]*22,held16=obs.state16))
    world_object=np.array(g['object_pose']).reshape(4,4)
    relative=np.array(g['current_tcp_pose_object']).reshape(4,4)
    assert np.allclose(world_object@relative,np.array(g['current_tcp_pose']).reshape(4,4),atol=1e-12)
