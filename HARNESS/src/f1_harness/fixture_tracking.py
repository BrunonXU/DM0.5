"""Current RGB-D aperture pose and rigid bucket relocation, without new calibration."""
import numpy as np
from .geometry import transform, align_square_pose
from .perception import measurement_region


def locate_fixture(depth,masks,config,reference_camera,reference_pose,anchor_config,*,identity_tolerance_m):
    """Register visible RGB-D anchors without needing the destination aperture.

    The origin is the registered fixture reference, not a newly measured hole.
    Reference camera extrinsics stay frozen while the current head pose may move.
    """
    from .geometry import crop_intrinsics
    inverse=np.linalg.inv(crop_intrinsics(config['k_rgb'],config['roi']))
    original_camera=transform(reference_camera);camera=transform(config['base_camera'])
    old=[];new=[];anchors={}
    for name,expected in anchor_config.items():
        mask=masks.get(name)
        if mask is None or mask.sum()<20:raise ValueError('fixture_relocalization_anchor_missing:'+name)
        y,x=np.nonzero(mask);values=depth[mask];values=values[np.isfinite(values)&(values>0)]
        if len(values)<20:raise ValueError('fixture_relocalization_depth_missing:'+name)
        center=np.array([x.mean(),y.mean()]);z=float(np.median(values))
        anchors[name]=dict(roi_center_px=center.tolist(),depth_m=z)
        a=(inverse@np.r_[expected['roi_center_px'],1])*expected['depth_m']
        b=(inverse@np.r_[center,1])*z
        old.append((original_camera@np.r_[a,1])[:3]);new.append((camera@np.r_[b,1])[:3])
    old,new=np.asarray(old),np.asarray(new)
    if len(old)<3 or np.linalg.matrix_rank(old-old.mean(0))<2 or np.linalg.matrix_rank(new-new.mean(0))<2:
        raise ValueError('fixture_anchors_must_be_noncollinear')
    pair_error=np.abs(np.linalg.norm(old[:,None]-old[None,:],axis=2)-np.linalg.norm(new[:,None]-new[None,:],axis=2))
    if pair_error.max()>identity_tolerance_m:raise ValueError('fixture_relocalization_anchor_identity_changed')
    a,b=old.mean(0),new.mean(0);u,_,vt=np.linalg.svd((old-a).T@(new-b))
    rotation=vt.T@u.T
    if np.linalg.det(rotation)<0:vt[-1]*=-1;rotation=vt.T@u.T
    delta=np.eye(4);delta[:3,:3]=rotation;delta[:3,3]=b-rotation@a
    errors=np.linalg.norm((old@rotation.T+delta[:3,3])-new,axis=1)
    return delta@transform(reference_pose),anchors,dict(source='rgbd_anchor_registration',
        anchor_count=len(old),anchor_distance_error_m=float(pair_error.max()),
        registration_residual_m=float(errors.max()),aperture_measured=False)


def locate_hole(mask,depth,masks,config,reference,anchor_config,*,identity_tolerance_m):
    import cv2
    from .geometry import crop_intrinsics
    reference=transform(reference);camera=transform(config['base_camera'])
    k=crop_intrinsics(config['k_rgb'],config['roi']);inverse=np.linalg.inv(k)
    anchors={};regions=[measurement_region(mask,dict(kind='outer_ring',inner_px=3,outer_px=8))]
    for name in anchor_config:
        region=masks.get(name)
        if region is None or region.sum()<20:raise ValueError('fixture_relocalization_anchor_missing:'+name)
        y,x=np.nonzero(region);values=depth[region];values=values[np.isfinite(values)&(values>0)]
        if len(values)<20:raise ValueError('fixture_relocalization_depth_missing:'+name)
        anchors[name]=dict(roi_center_px=[float(x.mean()),float(y.mean())],depth_m=float(np.median(values)))
        regions.append(region)
    old=[];new=[]
    for name,expected_anchor in anchor_config.items():
        old.append((inverse@np.r_[expected_anchor['roi_center_px'],1])*expected_anchor['depth_m'])
        new.append((inverse@np.r_[anchors[name]['roi_center_px'],1])*anchors[name]['depth_m'])
    old,new=np.asarray(old),np.asarray(new)
    pair_error=np.abs(np.linalg.norm(old[:,None]-old[None,:],axis=2)-np.linalg.norm(new[:,None]-new[None,:],axis=2))
    if len(old)<3 or pair_error.max()>identity_tolerance_m:
        raise ValueError('fixture_relocalization_anchor_identity_changed')
    contours,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    if len(contours)!=1:raise ValueError('fixture_relocalization_ambiguous_aperture')
    contour=contours[0];corners=cv2.approxPolyDP(contour,.03*cv2.arcLength(contour,True),True).reshape(-1,2)
    if len(corners)!=4:raise ValueError('fixture_relocalization_aperture_not_square')
    pose,details=square_on_lid(corners,depth,np.logical_or.reduce(regions),config,reference)
    details['anchor_distance_error_m']=float(pair_error.max())
    return pose,anchors,details


def square_on_lid(corners,depth,region,config,reference):
    """Intersect measured corner rays with the lid's current RGB-D support."""
    from .geometry import crop_intrinsics
    reference=transform(reference)
    camera=transform(config['base_camera'])
    inverse=np.linalg.inv(crop_intrinsics(config['k_rgb'],config['roi']))
    y,x=np.nonzero(region&np.isfinite(depth)&(depth>0));z=depth[y,x]
    if len(z)<60:raise ValueError('fixture_relocalization_depth_missing')
    points=(np.column_stack([x,y,np.ones(len(x))])@inverse.T)*z[:,None]
    # Fit the measured lid, rejecting background returns through apertures.
    for _ in range(3):
        center=np.median(points,axis=0)
        _,_,axes=np.linalg.svd(points-center,full_matrices=False);normal=axes[-1]
        residual=np.abs((points-center)@normal)
        points=points[residual<=max(.003,float(np.quantile(residual,.8)))]
    center=points.mean(0);_,_,axes=np.linalg.svd(points-center,full_matrices=False);normal=axes[-1]
    if np.quantile(np.abs((points-center)@normal),.95)>.003:
        raise ValueError('fixture_relocalization_lid_not_planar')
    expected=camera[:3,:3].T@reference[:3,2]
    if normal@expected<0:normal=-normal
    rays=np.column_stack([corners,np.ones(4)])@inverse.T
    denominator=rays@normal
    if np.min(np.abs(denominator))<.1:raise ValueError('fixture_relocalization_grazing_view')
    xyz=rays*((center@normal)/denominator)[:,None]
    if np.any(xyz[:,2]<=0):raise ValueError('fixture_relocalization_behind_camera')
    edges=np.roll(xyz,-1,axis=0)-xyz;lengths=np.linalg.norm(edges,axis=1)
    if lengths.min()<=0 or lengths.max()/lengths.min()>1.3:
        raise ValueError('fixture_relocalization_aperture_not_square')
    axis=edges[0]/lengths[0];axis-=normal*(axis@normal);axis/=np.linalg.norm(axis)
    pose=np.eye(4);pose[:3,:3]=np.column_stack([axis,np.cross(normal,axis),normal]);pose[:3,3]=xyz.mean(0)
    pose=camera@pose;pose,_=align_square_pose(pose,reference)
    return pose,dict(depth_points=len(points),aperture_sides_m=lengths.tolist(),
        plane_residual_m=float(np.quantile(np.abs((points-center)@normal),.95)),
        translation_from_reference_m=(pose[:3,3]-reference[:3,3]).tolist())
