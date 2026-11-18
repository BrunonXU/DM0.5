"""Instrument a saved native preview without changing its collision decisions."""
import argparse
import json
from pathlib import Path
import runpy
import sys
from dataclasses import replace
import numpy as np
from f1_harness import skills
from f1_harness.geometry import transform

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--diagnostic',type=Path,required=True)
a,forward=p.parse_known_args()
if a.diagnostic.exists():raise FileExistsError(a.diagnostic)
original=skills.check_motion_route
rows=[]
def check(scene,goals,**kwargs):
    before=transform(kwargs.get('start') if kwargs.get('start') is not None else scene.base_eef)
    attached=kwargs.get('eef_object')
    options={k:kwargs.get(k,False) for k in ('allow_target_contact','support_lift')}
    for i,raw in enumerate(goals):
        after=transform(raw)
        if not skills._motion_clear(scene,before,after,attached,**options):
            tip=transform(scene.eef_tip_reference)
            collisions=[]
            for obstacle in skills.stage_obstacles(scene,allow_target_contact=options['allow_target_contact']):
                one=replace(scene,obstacles=(obstacle,))
                if not skills._motion_clear(one,before,after,attached,**options):
                    collisions.append(dict(role=obstacle.get('role'),name=obstacle.get('name'),
                        start_clear=skills._motion_clear(one,before,before,attached,**options),
                        end_clear=skills._motion_clear(one,after,after,attached,**options)))
            rows.append(dict(variant=kwargs.get('variant'),route=kwargs.get('route_family'),
                segment=i,collisions=collisions,start_tcp=(before@tip).tolist(),
                end_tcp=(after@tip).tolist(),tool_boxes=scene.tool_local_boxes,
                tool_margin_m=skills.tool_margin(scene)))
            break
        before=after
    return original(scene,goals,**kwargs)
skills.check_motion_route=check
sys.argv=[str(Path(__file__).with_name('preview_saved_scene.py')),*forward]
try:runpy.run_path(sys.argv[0],run_name='__main__')
finally:
    with a.diagnostic.open('x') as f:json.dump(rows,f,indent=2)
    print(json.dumps([dict(route=r['route'],segment=r['segment'],collisions=r['collisions']) for r in rows]))
