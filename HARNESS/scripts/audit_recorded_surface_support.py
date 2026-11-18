"""Audit exact recorded geometry inputs, with a labeled fraction-only counterfactual.

Does not connect to a model, robot, owner or planner. No production settings change.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from f1_harness.geometry import masked_points, crop_intrinsics, fit_block_pose
from f1_harness.surface import select_upright_face


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args();report=json.loads((args.directory/'report.json').read_text());rows=[]
    for entry in report['measurement_inputs']:
        archive=np.load(args.directory/entry['file']);x0,y0,x1,y1=entry['roi']
        points=masked_points(archive['aligned_depth_m'][y0:y1,x0:x1],archive['mask'],
            crop_intrinsics(archive['k_rgb'],entry['roi']))
        transform=archive['base_camera'];world=points@transform[:3,:3].T+transform[:3,3]
        cfg=entry['surface_selection'];normal=np.asarray(cfg['normal_world']);heights=world@normal
        ordered=np.sort(heights);threshold=cfg['distance_threshold_m']
        ends=np.searchsorted(ordered,ordered+2*threshold,side='right')
        first=int(np.argmax(ends-np.arange(len(ordered))))
        height=float(np.median(ordered[first:ends[first]]));keep=np.abs(heights-height)<=threshold
        centre=world[keep].mean(0);_,_,axes=np.linalg.svd(world[keep]-centre,full_matrices=False)
        uv=(world[keep]-centre)@axes[:2].T
        spans=np.quantile(uv,.95,axis=0)-np.quantile(uv,.05,axis=0)
        row=dict(file=entry['file'],input_sha256=hashlib.sha256((args.directory/entry['file']).read_bytes()).hexdigest(),
            mask_px=int(archive['mask'].sum()),valid_depth_points=len(points),selected_points=int(keep.sum()),
            selected_fraction=float(keep.mean()),height_m=height,selected_planar_spans_m=spans.tolist(),
            selected_height_residual_p95_m=float(np.quantile(np.abs(heights[keep]-height),.95)),
            world_extents=np.quantile(world,[.05,.95],axis=0).tolist())
        try:
            selected,diagnostics=select_upright_face(points,transform,dict(cfg,minimum_inlier_fraction=1e-9))
            row['fraction_only_relaxed_surface']=diagnostics
            row['fraction_only_relaxed_fit']=fit_block_pose(selected,normal_prior=transform[:3,:3].T@normal)
        except ValueError as exc:row['fraction_only_relaxed_error']=str(exc)
        rows.append(row)
    result=dict(schema='readonly-original-geometry-support-audit-v1',production_config_changed=False,
        offline_variant='minimum_inlier_fraction=1e-9 only; all remaining checks unchanged',
        original_report_sha256=hashlib.sha256((args.directory/'report.json').read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),rows=rows)
    (args.directory/'surface-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    (args.directory/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps(dict(rows=len(rows),fraction_only_errors=[r.get('fraction_only_relaxed_error') for r in rows])))


if __name__=='__main__':main()
