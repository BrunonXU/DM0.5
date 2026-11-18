"""Per-query coordinate reuse must preserve each obstacle's own verdict."""
from itertools import product
import numpy as np
import pytest
from scipy.spatial.transform import Rotation
from f1_harness.geometry import _points_clear


def corners(low=-.1,high=.1):
    return np.array(list(product(*[(low,high)]*3)))


@pytest.mark.parametrize('sat',[False,True])
def test_grouped_shared_frames_equal_independent_obstacle_checks(sat):
    rng=np.random.default_rng(405)
    accepted=rejected=0
    for _ in range(180):
        frames=[]
        for j in range(3):
            frame=np.eye(4);frame[:3,:3]=Rotation.from_rotvec(rng.normal(size=3)).as_matrix()
            frame[:3,3]=rng.uniform(-.3,.3,3);frames.append(frame)
        points=corners()@frames[0][:3,:3].T+rng.uniform(-.2,.2,3)
        obstacles=[]
        for j in range(12):
            low=rng.uniform(-.4,.4,3);high=low+rng.uniform(.01,.2,3)
            obstacles.append(dict(min=low,max=high,pose=frames[j%3].copy()))
        axes=frames[0][:3,:3].T if sat else None
        padding=float(rng.uniform(0,.04))
        expected=all(_points_clear(points,padding,[o],separating_axes=axes) for o in obstacles)
        assert _points_clear(points,padding,obstacles,separating_axes=axes)==expected
        assert _points_clear(points,padding,list(reversed(obstacles)),separating_axes=axes)==expected
        accepted+=expected;rejected+=not expected
    assert accepted and rejected


@pytest.mark.parametrize('padding',[0.,.01])
def test_same_frame_different_bounds_and_exact_contact(padding):
    points=corners();edge=.1+padding
    far=dict(min=[2,2,2],max=[3,3,3])
    hit=dict(min=[edge,-.1,-.1],max=[1,.1,.1])
    clear=dict(hit,min=[np.nextafter(edge,np.inf),-.1,-.1])
    assert _points_clear(points,padding,[far,clear])
    assert not _points_clear(points,padding,[far,hit])


def test_new_call_does_not_reuse_points_padding_or_mutated_pose():
    points=corners();frame=np.eye(4)
    obstacle=dict(min=[.2,-.1,-.1],max=[.3,.1,.1],pose=frame)
    assert _points_clear(points,0,[obstacle,obstacle])
    assert not _points_clear(points,.1,[obstacle,obstacle])
    points[:,0]+=.15
    assert not _points_clear(points,0,[obstacle,obstacle])
    frame[0,3]=2
    assert _points_clear(points,0,[obstacle,obstacle])
    frame[0,0]=2
    with pytest.raises(ValueError,match='orthonormal'):_points_clear(points,0,[obstacle])


def test_many_frames_fall_back_without_losing_late_collision():
    obstacles=[]
    for j in range(40):
        frame=np.eye(4);frame[0,3]=j+1
        obstacles.append(dict(min=[0,0,0],max=[.1,.1,.1],pose=frame))
    points=corners()
    assert _points_clear(points,0,obstacles)
    obstacles[-1]['min']=[-40,-.1,-.1];obstacles[-1]['max']=[-39.9,.1,.1]
    assert not _points_clear(points,0,obstacles)


def test_halfspace_and_lazy_invalid_frame_behavior_are_unchanged():
    points=corners();far=dict(min=[2,2,2],max=[3,3,3])
    table=dict(type='halfspace',normal=[0,0,1],point=[0,0,-.1])
    assert _points_clear(points,0,[far,table,far])
    assert not _points_clear(points,.001,[far,table,far])
    invalid=dict(far,pose=np.zeros((4,4)))
    assert not _points_clear(points,.001,[table,invalid])
    with pytest.raises(ValueError):_points_clear(points,0,[far,invalid])


def test_sub_tolerance_frame_change_is_not_merged():
    points=corners();far_frame=np.eye(4);far_frame[0,3]=1e-12
    clear=dict(min=[.1,-.1,-.1],max=[1,.1,.1],pose=far_frame)
    hit=dict(clear,pose=np.eye(4))
    assert _points_clear(points,0,[clear])
    assert not _points_clear(points,0,[clear,hit])
