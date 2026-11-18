import numpy as np
import pytest
from f1_harness.geometry import crop_intrinsics, eef_goal, register_depth, swept_box_clear
from f1_harness.calibration import solve_tcp, solve_handeye, validate_bundle
from scipy.spatial.transform import Rotation


def test_roi_preserves_camera_rays():
    k = np.array([[700.,0,640],[0,700,360],[0,0,1]])
    cropped = crop_intrinsics(k)
    assert np.allclose(np.linalg.solve(k, [500,300,1]), np.linalg.solve(cropped, [193,272,1]))


def test_in_hand_offset_is_applied():
    target, held = np.eye(4), np.eye(4)
    target[:3,3] = [.4,.2,.1]; held[:3,3] = [.01,.02,.03]
    assert np.allclose(eef_goal(target,held) @ held, target)


def test_depth_registration_metric_and_empty():
    k = np.array([[100.,0,1],[0,100,1],[0,0,1]])
    depth = np.full((3,3),500,np.uint16)
    result = register_depth(depth,k,[],np.eye(4),k,[],(3,3),.001)
    assert np.allclose(result,.5)
    assert not register_depth(depth*0,k,[],np.eye(4),k,[],(3,3),.001).any()


def test_collision_sweep_includes_attached_extent():
    obstacles = [{"min":[.5,.04,-.1],"max":[.6,.1,.1]}]
    assert not swept_box_clear([0,0,0],[1,0,0],[.05,.05,.05],obstacles,uncertainty_m=.01)


def poses(n):
    rng = np.random.default_rng(49)
    result=[]
    for _ in range(n):
        t=np.eye(4);t[:3,:3]=Rotation.from_rotvec(rng.normal(0,.4,3)).as_matrix();t[:3,3]=rng.uniform(-.3,.3,3)
        result.append(t)
    return result


def test_pivot_tcp_recovers_independent_geometry():
    tcp,pivot=np.array([.02,-.01,.12]),np.array([.4,.1,.3])
    values=poses(12)
    for t in values:t[:3,3]=pivot-t[:3,:3]@tcp
    result=solve_tcp([t.tolist() for t in values[:8]],[t.tolist() for t in values[8:]],max_error_m=.001)
    assert result['accepted']
    assert np.allclose(result['eef_tcp_translation_m'],tcp)
    with pytest.raises(ValueError,match="independent"):
        solve_tcp([t.tolist() for t in values[:8]],[t.tolist() for t in values[:3]],max_error_m=.001)


def test_handeye_convention_recovers_transform():
    eef_camera=poses(1)[0];base_board=np.eye(4);base_board[:3,3]=[.5,.1,.2]
    rows=[dict(base_eef=t.tolist(),camera_board=(np.linalg.inv(eef_camera)@np.linalg.inv(t)@base_board).tolist()) for t in poses(15)]
    result=solve_handeye(rows[:11],rows[11:],max_translation_m=.001,max_rotation_deg=.1)
    assert result['accepted']
    assert np.allclose(result['eef_camera'],eef_camera)


def test_empty_bundle_never_authorizes():
    assert not validate_bundle({})['accepted']


@pytest.mark.parametrize('z,depth,expected', [
    (-.012, .012, True),
    (-.011999917580, .012, True),  # Actual approved-output FK endpoint, 82.42 nm short.
    (-.012003394, .012, True),
    (-.011998, .012, False),      # 2 um short is outside numerical equality.
    (-.0119, .012, False),       # 0.1 mm short must not become insertion evidence.
    (-.004, .012, False),
    (float('nan'), .012, False),
    (-float('inf'), .012, False),
    (-.012, 0., False),
    (-.012, float('nan'), False),
])
def test_insertion_depth_uses_absolute_numerical_boundary_only(z, depth, expected):
    from f1_harness.geometry import insertion_depth_reached
    assert insertion_depth_reached(z, depth) is expected


@pytest.mark.parametrize('z,expected',[(-.011977581368735613,True),(-.010001,True),
    (-.0099,False),(-.004,False),(0.,False)])
def test_coarse_depth_band_accepts_goal_residual_not_uninserted_object(z,expected):
    from f1_harness.geometry import insertion_depth_reached
    assert insertion_depth_reached(z,.012,tolerance_m=.002) is expected


@pytest.mark.parametrize('band',[-.001,.012,.02,float('nan'),float('inf'),True])
def test_invalid_depth_band_does_not_prove_insertion(band):
    from f1_harness.geometry import insertion_depth_reached
    assert not insertion_depth_reached(-.012,.012,tolerance_m=band)
