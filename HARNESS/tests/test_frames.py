import numpy as np
import pytest
from f1_harness.frames import FrameChain


def test_two_sided_frame_conversion_preserves_physical_object_target():
    # Native base is displaced/rotated; native EEF has a separate offset from the IK link.
    world_native=np.array([[0.,-1,0,2],[1,0,0,3],[0,0,1,4],[0,0,0,1]])
    link_native=np.eye(4);link_native[0,3]=.1
    native_eef=np.eye(4);native_eef[:3,3]=[.3,.4,.5]
    native_eef_object=np.eye(4);native_eef_object[2,3]=.2
    world_link=world_native@native_eef@np.linalg.inv(link_native)
    frames=FrameChain(world_native,link_native,'native','world')
    np.testing.assert_allclose(frames.native_eef(world_link),native_eef,atol=1e-12)
    actual_object=world_link@frames.ik_eef_object(native_eef_object)
    np.testing.assert_allclose(actual_object,frames.world_pose(native_eef@native_eef_object))
    grasp=np.linalg.inv(native_eef_object)
    np.testing.assert_allclose(actual_object@frames.object_ik_eef(grasp),world_link)
    obstacle=frames.world_obstacles([dict(min=[0,0,0],max=[1,2,3])])[0]
    np.testing.assert_allclose(obstacle['min'],[0,3,4]);np.testing.assert_allclose(obstacle['max'],[2,4,7])


def test_frame_names_cannot_replace_measured_conversion():
    with pytest.raises(ValueError,match='independently_verified'):
        FrameChain.load(dict(native_frame='native',ik_world_frame='native',accepted=True))
