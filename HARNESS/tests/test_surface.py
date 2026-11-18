import numpy as np
import pytest

from f1_harness.surface import select_upright_face


def configuration():
    return dict(kind='upright_planar_face',normal_world=[0,0,1],normal_tolerance_deg=15,
                distance_threshold_m=.003,minimum_points=100,minimum_inlier_fraction=.25,
                minimum_planar_span_m=.015,competing_support_ratio=.5)


def face(z, n=25):
    x,y=np.meshgrid(np.linspace(-.015,.015,n),np.linspace(-.015,.015,n))
    return np.column_stack((x.ravel(),y.ravel(),np.full(x.size,z)))


def test_top_selected_from_top_and_sides_without_resizing():
    top=face(.06)
    x,z=np.meshgrid(np.linspace(-.015,.015,25),np.linspace(0,.055,25))
    side=np.column_stack((x.ravel(),np.full(x.size,-.015),z.ravel()))
    selected,report=select_upright_face(np.concatenate((top,side)),np.eye(4),configuration())
    assert np.all(selected[:,2]>.056)
    assert len(selected)==len(top)
    assert report['inlier_fraction_of_mask_points']==.5
    assert report['residual_p95_m']<1e-10
    assert not report['physical_accuracy_certified']


def test_parallel_competing_faces_are_ambiguous():
    with pytest.raises(ValueError,match='ambiguous_parallel_object_faces'):
        select_upright_face(np.concatenate((face(.06),face(.03))),np.eye(4),configuration())


def test_world_normal_respects_camera_rotation():
    t=np.eye(4);t[:3,:3]=np.array([[1,0,0],[0,0,-1],[0,1,0]])
    camera=face(.06)@t[:3,:3]
    _,report=select_upright_face(camera,t,configuration())
    np.testing.assert_allclose(report['normal_world'],[0,0,1],atol=1e-10)


def test_tilted_face_rejected_in_upright_task():
    p=face(.06);p[:,2]+=p[:,0]
    with pytest.raises(ValueError,match='supported_upright_face_not_observed'):
        select_upright_face(p,np.eye(4),configuration())


def test_narrow_or_sparse_surface_not_reported_as_whole_face():
    p=face(.06);p[:,0]*=.1
    with pytest.raises(ValueError,match='observed_face_is_too_narrow'):
        select_upright_face(p,np.eye(4),configuration())
    with pytest.raises(ValueError,match='surface_points_missing_or_invalid'):
        select_upright_face(face(.06,5),np.eye(4),configuration())


@pytest.mark.parametrize('key,value',[
    ('normal_world',[0,0,0]),('normal_world',[0,0,float('nan')]),
    ('distance_threshold_m',0),('normal_tolerance_deg',90),
    ('minimum_points',True),('minimum_inlier_fraction',0),('competing_support_ratio',2),
])
def test_invalid_configuration_rejected(key,value):
    cfg=configuration();cfg[key]=value
    with pytest.raises(ValueError,match='invalid_surface_selection_parameters'):
        select_upright_face(face(.06),np.eye(4),cfg)


def test_mask_to_object_uses_selected_surface_and_preserves_evidence():
    from f1_harness.scene import object_from_mask
    depth=np.zeros((120,120),np.float32)
    depth[35:65,35:75]=.5
    depth[65:85,35:75]=np.linspace(.51,.56,20)[:,None]
    mask=depth>0
    base_camera=np.diag([1.,-1.,-1.,1.]);base_camera[2,3]=1
    diagnostics={}
    obj=object_from_mask(object_id='block',shape='square',aligned_depth_m=depth,mask=mask,
        k_rgb=[[500.,0.,60.],[0.,500.,60.],[0.,0.,1.]],base_camera=base_camera,
        calibration_error_m=.01,evidence_ids=('rgb','depth'),frame_id='world',roi=[0,0,120,120],
        surface_selection=configuration(),diagnostics=diagnostics)
    assert obj.evidence_ids==('rgb','depth')
    assert abs(np.asarray(obj.pose).reshape(4,4)[2,3]-.5)<1e-6
    assert diagnostics['surface_selection']['inlier_points']==1200
    assert obj.uncertainty_m>=.01


def test_registered_upright_pose_prior_keeps_measured_position_and_residual():
    from f1_harness.geometry import fit_block_pose
    cloud=face(.06);cloud[:,2]+=cloud[:,0]*.12
    measured=fit_block_pose(cloud)
    supported=fit_block_pose(cloud,normal_prior=[0,0,1])
    np.testing.assert_allclose(np.asarray(supported['pose'])[:3,2],[0,0,1],atol=1e-12)
    np.testing.assert_allclose(np.asarray(supported['pose'])[:3,3],np.asarray(measured['pose'])[:3,3])
    assert supported['residual_p95_m']>measured['residual_p95_m']
    with pytest.raises(ValueError,match='reliable planar'):
        fit_block_pose(cloud*np.array([1,1,10]),normal_prior=[0,0,1])


def test_upright_prior_still_requires_observed_support_and_rejects_thin_tilt_slice():
    cfg=configuration()|{'pose_normal_prior':'registered_upright'}
    selected,report=select_upright_face(face(.06),np.eye(4),cfg)
    assert report['normal_basis']=='registered_upright_prior' and len(selected)==625
    tilted=face(.06);tilted[:,2]+=tilted[:,0]
    with pytest.raises(ValueError,match='ambiguous_parallel_object_faces|observed_face_is_too_narrow'):
        select_upright_face(tilted,np.eye(4),cfg)


def test_held_tilt_uses_association_direction_but_measures_actual_face():
    from scipy.spatial.transform import Rotation
    rotation=Rotation.from_euler('y',19.36,degrees=True).as_matrix()
    points=face(.06)@rotation.T
    with pytest.raises(ValueError,match='supported_upright_face_not_observed'):
        select_upright_face(points,np.eye(4),configuration())
    # The association prior is deliberately imperfect. The fitted normal must
    # come from depth rather than simply echoing that prior.
    predicted=Rotation.from_euler('y',18,degrees=True).as_matrix()[:,2]
    _,report=select_upright_face(points,np.eye(4),configuration()|{'normal_world':predicted.tolist()})
    np.testing.assert_allclose(report['normal_world'],rotation[:,2],atol=1e-8)
    assert report['normal_basis']=='measured_plane'


def test_rejected_surface_reports_real_shape_failure_and_fraction_warning():
    cfg=configuration()|{'pose_normal_prior':'registered_upright'}
    points=face(.06);points[:,2]+=points[:,0]
    report={}
    with pytest.raises(ValueError,match='ambiguous_parallel_object_faces'):
        select_upright_face(points,np.eye(4),cfg,diagnostics=report)
    assert report['input_points']==625
    assert report['inlier_points']>=cfg['minimum_points']
    assert report['absolute_support_sufficient']
    assert not report['fractional_support_sufficient']
    assert 'insufficient_upright_face_support' not in report['failed_checks']
    assert report['quality_warnings']==['planar_support_fraction_below_reference']
    assert not report['fraction_used_as_admission_gate']
    assert 'observed_face_is_too_narrow' in report['failed_checks']
    assert min(report['observed_planar_spans_m'])<cfg['minimum_planar_span_m']
    assert report['residual_p95_m']<=cfg['distance_threshold_m']
    assert report['competing_inlier_points']>=0
    assert report['diagnostic_only'] and not report['accepted']
    assert not report['physical_accuracy_certified']


def test_small_relative_support_does_not_reject_a_measured_full_face():
    # A large visible side changes the mask denominator, not the measured top.
    top=face(.3)
    x,z=np.meshgrid(np.linspace(-.015,.015,25),np.linspace(-.3,.2,125))
    side=np.column_stack((x.ravel(),np.full(x.size,-.015),z.ravel()))
    selected,report=select_upright_face(np.concatenate((top,side)),np.eye(4),
        configuration()|{'pose_normal_prior':'registered_upright'})
    assert len(selected)==len(top)
    assert report['inlier_fraction_of_mask_points']<configuration()['minimum_inlier_fraction']
    assert report['accepted'] and not report['failed_checks']
    assert report['quality_warnings']==['planar_support_fraction_below_reference']
    actual={tuple(point) for point in np.concatenate((top,side))}
    anchors=report['surface_anchor']['extremal_points_world']
    assert len(anchors)==6 and all(tuple(point) in actual for point in anchors)


def test_mask_geometry_keeps_rejected_surface_diagnostics_and_no_pose():
    from f1_harness.scene import object_from_mask
    depth=np.zeros((120,120),np.float32);depth[35:75,48:56]=.5
    diagnostics={}
    with pytest.raises(ValueError,match='observed_face_is_too_narrow'):
        object_from_mask(object_id='block',shape='square',aligned_depth_m=depth,mask=depth>0,
            k_rgb=[[500.,0.,60.],[0.,500.,60.],[0.,0.,1.]],base_camera=np.eye(4),
            calibration_error_m=.01,evidence_ids=('rgb','depth'),frame_id='world',roi=[0,0,120,120],
            surface_selection=configuration(),diagnostics=diagnostics)
    report=diagnostics['surface_selection']
    assert report['input_points']==320
    assert report['failed_checks']==['observed_face_is_too_narrow']
    assert not report['accepted']
    assert 'observed_face_fit' not in diagnostics


def test_narrow_exterior_plane_is_independent_of_prior_and_all_depth_counterevidence():
    from f1_harness.partial_scene import registered_box_surface_anchor
    pose=np.diag([1.,-1.,-1.,1.]);pose[2,3]=1.
    world=face(.06);world[:,0]*=.1
    config=configuration()|{'pose_normal_prior':'registered_upright'}
    def measure(cloud):
        report={}
        with pytest.raises(ValueError,match='observed_face_is_too_narrow'):
            select_upright_face((cloud-pose[:3,3])@pose[:3,:3],pose,config,diagnostics=report)
        return report
    report=measure(world)
    anchor=registered_box_surface_anchor(report,.015)
    assert anchor['supporting_plane']['camera_plane_signed_distance_m']>0
    np.testing.assert_allclose(anchor['supporting_plane']['normal_world'],[0,0,1],atol=1e-12)
    # One real target return above the plane contradicts the solid halfspace;
    # it must not be discarded as an outlier merely to enable motion.
    contradictory=measure(np.vstack((world,[0,0,.09])))
    assert contradictory['supporting_plane_candidate']['all_target_front_side_max_m']>.015
    assert 'supporting_plane' not in registered_box_surface_anchor(contradictory,.015)
    # A thin horizontal prior slice through a sloping side has a good height
    # residual but no independently observed normal agreeing with the prior.
    sloping=world.copy();sloping[:,2]+=sloping[:,0]
    mismatch=measure(sloping)
    assert mismatch['surface_anchor'] and 'supporting_plane_candidate' not in mismatch
