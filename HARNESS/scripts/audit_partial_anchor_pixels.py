"""Show exact archived plane-consensus pixels; never fit or authorize an object pose."""
import argparse
import hashlib
import inspect
import json
from pathlib import Path

import cv2
import numpy as np
import f1_harness.surface as surface
from f1_harness.contracts import Frame
from f1_harness.geometry import masked_points,crop_intrinsics
from f1_harness.images import decode
from replay_sam_identity import rectified_pixels


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--index',type=int,required=True)
    p.add_argument('--blob-root',type=Path,required=True);p.add_argument('--site',type=Path,required=True)
    args=p.parse_args();root=args.report.parent;report=json.loads(args.report.read_text())
    entry=report['measurement_inputs'][args.index];data=np.load(root/entry['file'])
    frames=[]
    for row in report.get('rows',[]):frames.extend(row['observation']['frames'])
    for row in report.get('geometry',[]):frames.extend((row.get('output_observation') or {}).get('frames',[]))
    frame=Frame.model_validate(next(f for f in frames if f['blob_sha256']==entry['evidence_ids'][0]))
    geometry=json.loads((args.site/'gateway.local.json').read_text())['scene']['geometry']
    config=geometry if frame.camera=='right' else geometry['hole_camera']
    rgb=rectified_pixels(decode(frame,(args.blob_root/frame.blob_sha256).read_bytes()),config)
    x0,y0,x1,y1=entry['roi'];depth=data['aligned_depth_m'][y0:y1,x0:x1]
    points=masked_points(depth,data['mask'],crop_intrinsics(data['k_rgb'],entry['roi']))
    # Extract the exact already-computed inlier set even when final pose support
    # rejects it. Only this diagnostic copy's final raise is bypassed; all
    # selection, checks, thresholds and reported rejection reasons are retained.
    source=inspect.getsource(surface.select_upright_face)
    assert source.count('if failed:raise ValueError(failed[0])')==1
    namespace=dict(vars(surface));exec(source.replace('if failed:raise ValueError(failed[0])',''),namespace)
    selected,metrics=namespace['select_upright_face'](points,data['base_camera'],entry['surface_selection'])
    if metrics.get('surface_anchor') is None:raise ValueError('no_qualified_surface_anchor_to_plot')
    values={tuple(row) for row in selected};keep=np.array([tuple(row) in values for row in points])
    yy,xx=np.nonzero(data['mask']&np.isfinite(depth)&(depth>0))
    world=selected@data['base_camera'][:3,:3].T+data['base_camera'][:3,3]
    selected_indices=np.flatnonzero(keep);extrema=[]
    for axis in range(3):
        for side,fn in [('min',np.argmin),('max',np.argmax)]:
            index=int(fn(world[:,axis]));pixel_index=selected_indices[index]
            extrema.append(dict(axis=axis,side=side,point_world=world[index].tolist(),
                rectified_roi_pixel=[int(xx[pixel_index]),int(yy[pixel_index])]))
    centre=world.mean(0);_,singular,axes=np.linalg.svd(world-centre,full_matrices=False)
    normal=axes[-1];selected_normal=np.asarray(metrics['normal_world'])
    if normal@selected_normal<0:normal=-normal
    all_world=points@data['base_camera'][:3,:3].T+data['base_camera'][:3,3]
    signed=(all_world-centre)@normal
    margin=geometry['planning_margin_m'];cosine=float(np.clip(normal@selected_normal,-1,1))
    plane=dict(centre_world=centre.tolist(),independent_pca_normal_world=normal.tolist(),
        selected_normal_world=selected_normal.tolist(),normal_difference_deg=float(np.degrees(np.arccos(cosine))),
        configured_normal_tolerance_deg=entry['surface_selection']['normal_tolerance_deg'],
        camera_plane_signed_distance_m=float((data['base_camera'][:3,3]-centre)@normal),
        camera_selected_normal_signed_distance_m=float((data['base_camera'][:3,3]-centre)@selected_normal),
        all_target_front_side_max_m=float(signed.max()),all_target_front_side_min_m=float(signed.min()),
        all_target_points_beyond_existing_margin=int(np.sum(signed>margin)),existing_margin_m=margin,
        independent_pca_residual_p95_m=float(np.quantile(np.abs((world-centre)@normal),.95)),
        singular_values=singular.tolist(),prior=entry['surface_selection'].get('pose_normal_prior'),
        no_runtime_halfspace_added=True)
    contours,_=cv2.findContours(data['mask'].astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(rgb,contours,-1,(255,0,255),1);rgb[yy[keep],xx[keep]]=[0,255,0]
    prefix=root/('anchor-pixels-'+str(args.index))
    cv2.imwrite(str(prefix)+'.png',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
    my,mx=np.nonzero(data['mask']);crop=rgb[max(0,my.min()-30):min(rgb.shape[0],my.max()+31),max(0,mx.min()-30):min(rgb.shape[1],mx.max()+31)]
    cv2.imwrite(str(prefix)+'-zoom.png',cv2.cvtColor(cv2.resize(crop,None,fx=4,fy=4,interpolation=cv2.INTER_NEAREST),cv2.COLOR_RGB2BGR))
    result=dict(schema='f1-partial-anchor-pixel-audit-v1',source_frame=frame.model_dump(),
        input_file=entry['file'],input_sha256=hashlib.sha256((root/entry['file']).read_bytes()).hexdigest(),
        surface_function_sha256=hashlib.sha256(source.encode()).hexdigest(),
        extractor_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        diagnostic_extraction='bypass final raise only, preserve exact selected points and reported failed checks; never fit pose',
        source_ns=frame.source_ns,selected_pixels=int(keep.sum()),metrics=metrics,
        actual_extremal_points=extrema,
        measured_face_halfspace_diagnostic=plane,
        display=dict(mask_outline='magenta',measured_plane_inlier_pixels='green'),physical_accuracy_certified=False)
    Path(str(prefix)+'.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(output=str(prefix),selected_pixels=int(keep.sum()),failed_checks=metrics['failed_checks'])))


if __name__=='__main__':main()
