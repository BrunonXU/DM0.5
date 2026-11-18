"""Explicit estimated fixture construction; output is configuration, never calibration acceptance."""
from itertools import product
import numpy as np
from .geometry import transform


def bucket_obstacles(bounds,base_hole,opening_xy_m,*,wall_thickness_m,lid_thickness_m):
    """Retain four walls, bottom, aperture rim and a tool-only solid lid.

    Thicknesses are explicit configuration estimates. Hole axes stay oriented;
    expanding each rim into world AABBs would incorrectly close a narrow hole.
    """
    pose=transform(base_hole);low,high=np.asarray(bounds['min']),np.asarray(bounds['max'])
    opening=np.asarray(opening_xy_m,float)/2
    if (low.shape!=(3,) or high.shape!=(3,) or opening.shape!=(2,) or not np.isfinite(opening).all()
            or np.any(low>=high) or np.any(opening<=0) or not 0<wall_thickness_m<.03
            or not 0<lid_thickness_m<.03):raise ValueError('invalid_bucket_fixture')
    points=(np.array(list(product(*zip(low,high))))-pose[:3,3])@pose[:3,:3]
    lo,hi=points.min(0),points.max(0);x0,y0,z0=lo;x1,y1,z1=hi;hx,hy=opening
    if not (x0<-hx<hx<x1 and y0<-hy<hy<y1 and z0<-lid_thickness_m<0<z1):
        raise ValueError('opening_outside_bucket')
    wall=wall_thickness_m;lid=lid_thickness_m
    items=[('wall_x_low',[x0,y0,z0],[x0+wall,y1,z1]),
           ('wall_x_high',[x1-wall,y0,z0],[x1,y1,z1]),
           ('wall_y_low',[x0,y0,z0],[x1,y0+wall,z1]),
           ('wall_y_high',[x0,y1-wall,z0],[x1,y1,z1]),
           ('bottom',[x0,y0,z0],[x1,y1,z0+wall]),
           ('rim_x_low',[x0,y0,-lid],[-hx,y1,0]),
           ('rim_x_high',[hx,y0,-lid],[x1,y1,0]),
           ('rim_y_low',[-hx,y0,-lid],[hx,-hy,0]),
           ('rim_y_high',[-hx,hy,-lid],[hx,y1,0]),
           ('lid_tool_only',[x0,y0,-lid],[x1,y1,0])]
    return [dict(min=a,max=b,pose=pose.tolist(),role='bucket',fixture_part=name,
                 collision_scope='tool_only' if name=='lid_tool_only' else 'all') for name,a,b in items]


def table_halfspace(normal,point):
    normal=np.asarray(normal,float);point=np.asarray(point,float)
    if normal.shape!=(3,) or point.shape!=(3,) or not np.isfinite([normal,point]).all() or normal[2]<=0:
        raise ValueError('invalid_table_plane')
    normal/=np.linalg.norm(normal)
    return dict(type='halfspace',role='table',normal=normal.tolist(),point=point.tolist())


def grasp_eef_offset(base_object,base_eef,estimated_tip,*,height_above_bottom_m,approach='preserve_wrist'):
    """Estimate pregrasp pose while preserving initial wrist orientation.

    This empty-tool fit is not a loaded TCP. Subsequent transfer always consumes
    the freshly observed eef_object transform instead.
    """
    obj,eef,tip=map(transform,(base_object,base_eef,estimated_tip))
    if not np.isfinite(height_above_bottom_m) or height_above_bottom_m<=0:
        raise ValueError('positive_grasp_height_required')
    if approach=='object_normal':
        candidates=[]
        for axis in (obj[:3,0],obj[:3,1],-obj[:3,0],-obj[:3,1]):
            rotation=np.column_stack([axis,obj[:3,2],np.cross(axis,obj[:3,2])])
            angle=np.arccos(np.clip((np.trace(eef[:3,:3].T@rotation)-1)/2,-1,1))
            candidates.append((angle,rotation))
        eef=eef.copy();eef[:3,:3]=min(candidates,key=lambda item:item[0])[1]
    elif approach!='preserve_wrist':raise ValueError('unregistered_grasp_approach')
    result=np.eye(4);result[:3,:3]=obj[:3,:3].T@eef[:3,:3]
    result[:3,3]=np.array([0.,0.,height_above_bottom_m])-result[:3,:3]@tip[:3,3]
    return result.tolist()
