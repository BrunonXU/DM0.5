"""Analytic halfspace clearance for the declared linear/shortest-rotation pose arc.

Diagnostic only: no force or physical continuous-joint-motion claim.
"""
import math
import numpy as np
from scipy.spatial.transform import Rotation
from f1_harness.geometry import transform


def plane_clearance(start,end,local_corners,normal,point):
    a,b=transform(start),transform(end)
    corners=np.asarray(local_corners,float);n=np.asarray(normal,float);p=np.asarray(point,float)
    if corners.ndim!=2 or corners.shape[1]!=3 or not len(corners) or n.shape!=(3,) or p.shape!=(3,) or not np.isfinite(np.r_[corners.ravel(),n,p]).all() or np.linalg.norm(n)<=0:
        raise ValueError('invalid_plane_clearance_inputs')
    # Preserve the registered plane's scale, matching the existing predicate.
    omega=Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).as_rotvec();angle=float(np.linalg.norm(omega))
    translation=float((b[:3,3]-a[:3,3])@n)
    initial=(corners@a[:3,:3].T+a[:3,3]-p)@n
    final=(corners@b[:3,:3].T+b[:3,3]-p)@n
    minima=[]
    if angle<=1e-12:
        minima=[dict(corner=i,distance_m=float(min(x,y)),fraction=0. if x<=y else 1.) for i,(x,y) in enumerate(zip(initial,final))]
    else:
        axis=omega/angle
        for i,corner in enumerate(corners):
            parallel=axis*np.dot(axis,corner);perp=corner-parallel
            A=float(n@a[:3,:3]@perp);B=float(n@a[:3,:3]@np.cross(axis,corner))
            C=float(n@(a[:3,3]-p+a[:3,:3]@parallel));radius=math.hypot(A,B)
            angles=[0.,angle]
            if radius>0:
                ratio=-translation/(angle*radius)
                if -1<=ratio<=1:
                    phase=math.atan2(A,B);root=math.acos(ratio)
                    for sign in (-1,1):
                        for k in range(-2,3):
                            u=sign*root-phase+2*k*math.pi
                            if 0<u<angle:angles.append(u)
            candidates=[(C+translation*u/angle+A*math.cos(u)+B*math.sin(u),u/angle) for u in angles]
            value,fraction=min(candidates)
            minima.append(dict(corner=i,distance_m=value,fraction=fraction))
    witness=min(minima,key=lambda r:r['distance_m'])
    return dict(initial_min_m=float(initial.min()),final_min_m=float(final.min()),
        arc_min_m=witness['distance_m'],arc_min_fraction=witness['fraction'],corner=witness['corner'],
        downward_excursion_m=max(0.,float(initial.min())-witness['distance_m']),
        interpolation='linear_translation_shortest_rotation',physical_contact_proven=False)
