"""Registration optimization is pixel/metric equivalent to the OpenCV reference."""
import cv2
import numpy as np
import pytest

from f1_harness.geometry import _depth_rays, _project_color, register_depth


@pytest.mark.parametrize('count',[0,4,5,8,12,14])
def test_projection_without_jacobian_matches_opencv(count):
    rng=np.random.default_rng(4)
    points=rng.uniform([-.4,-.3,.5],[.4,.3,2.],size=(1000,3))
    k=np.array([[394.,0,324.],[0,393.,234.],[0,0,1.]])
    d=np.array([-.05,.05,.0005,.0001,-.019,.001,.002,.003,0.,0.,0.,0.,.005,-.005])[:count]
    expected=cv2.projectPoints(points,np.zeros(3),np.zeros(3),k,d)[0].reshape(-1,2)
    assert np.max(np.abs(_project_color(points,k,d)-expected))<1e-10


def test_depth_rays_cache_includes_shape_intrinsics_distortion_and_is_bounded():
    _depth_rays.cache_clear()
    k=(20.,0,10.,0,20.,10.,0,0,1.)
    a=_depth_rays((20,20),k,(0.,)*5)
    assert _depth_rays((20,20),k,(0.,)*5) is a and not a.flags.writeable
    assert _depth_rays((20,21),k,(0.,)*5).shape==(20,21,2)
    assert not np.array_equal(_depth_rays((20,20),(21.,*k[1:]),(0.,)*5),a)
    assert not np.array_equal(_depth_rays((20,20),k,(.01,0.,0.,0.,0.)),a)
    for i in range(12):_depth_rays((2,2),(20.+i,*k[1:]),(0.,)*5)
    assert _depth_rays.cache_info().currsize<=8


def test_registration_preserves_reference_zbuffer():
    rng=np.random.default_rng(6)
    depth=rng.integers(500,1500,size=(40,60),dtype=np.uint16);depth[::7,::5]=0
    kd=np.array([[100.,0,30.],[0,101.,20.],[0,0,1.]])
    kr=np.array([[98.,0,29.],[0,99.,19.],[0,0,1.]])
    dd=np.array([-.05,.02,.001,.002,-.01]);dr=dd*.8
    t=np.eye(4);t[:3,3]=[.01,.002,.003]
    y,x=np.nonzero(depth)
    rays=cv2.undistortPoints(np.column_stack((x,y)).astype(float).reshape(-1,1,2),kd,dd).reshape(-1,2)
    z=depth[y,x]*.001
    points=np.column_stack((rays*z[:,None],z))+t[:3,3]
    uv=np.rint(cv2.projectPoints(points,np.zeros(3),np.zeros(3),kr,dr)[0].reshape(-1,2)).astype(int)
    valid=(uv[:,0]>=0)&(uv[:,0]<60)&(uv[:,1]>=0)&(uv[:,1]<40)
    expected=np.full((40,60),np.inf)
    np.minimum.at(expected,(uv[valid,1],uv[valid,0]),points[valid,2]);expected[~np.isfinite(expected)]=0
    actual=register_depth(depth,kd,dd,t,kr,dr,(60,40),.001)
    np.testing.assert_allclose(actual,expected.astype(np.float32),atol=1e-7,rtol=0)


def test_rotated_ray_cache_does_not_cache_depth_or_extrinsic_translation():
    from f1_harness.geometry import _rgb_depth_rays
    _rgb_depth_rays.cache_clear()
    k=np.array([[30.,0,10.],[0,30.,10.],[0,0,1.]])
    depth=np.full((20,20),1000,dtype=np.uint16)
    t=np.eye(4);angle=.07
    t[:3,:3]=[[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]]
    a=register_depth(depth,k,[],t,k,[],(20,20),.001)
    assert _rgb_depth_rays.cache_info().misses==1
    t[2,3]=.02
    b=register_depth(depth+100,k,[],t,k,[],(20,20),.001)
    assert _rgb_depth_rays.cache_info().hits==1
    assert not np.array_equal(a,b)
    rays=_rgb_depth_rays((20,20),tuple(k.ravel()),(),tuple(t[:3,:3].ravel()))
    assert not rays.flags.writeable
    changed=t.copy();changed[:3,:3]=np.eye(3)
    register_depth(depth,k,[],changed,k,[],(20,20),.001)
    assert _rgb_depth_rays.cache_info().misses==2
