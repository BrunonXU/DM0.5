"""URDF FK and conservative AABB ground exclusion. No ROS, no robot commands."""
import functools,itertools,json,math
from pathlib import Path

def mul(a,b):return [[sum(a[i][k]*b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
def identity():return [[float(i==j) for j in range(4)] for i in range(4)]
def rotation(axis,angle):
    length=math.sqrt(sum(v*v for v in axis));x,y,z=[v/length for v in axis];c=math.cos(angle);s=math.sin(angle);v=1-c
    return [[x*x*v+c,x*y*v-z*s,x*z*v+y*s,0],[y*x*v+z*s,y*y*v+c,y*z*v-x*s,0],[z*x*v-y*s,z*y*v+x*s,z*z*v+c,0],[0,0,0,1]]
def origin(xyz,rpy):
    r,p,y=rpy;m=mul(mul(rotation([0,0,1],y),rotation([0,1,0],p)),rotation([1,0,0],r))
    for i in range(3):m[i][3]=xyz[i]
    return m
@functools.lru_cache(1)
def model():return json.loads(Path(__file__).with_name('model_geometry.json').read_text())
def frames(pose):
    d=model();angles={d['joint_map'][n]:math.radians(v*d['joint_signs'].get(n,1)) for n,v in pose.items() if n in d['joint_map']};out={'base_link':identity()};pending=list(d['joints'])
    while pending:
        progress=False
        for j in pending[:]:
            if j['parent'] not in out:continue
            out[j['child']]=mul(mul(out[j['parent']],origin(j['xyz'],j['rpy'])),rotation(j['axis'],angles.get(j['name'],0)));pending.remove(j);progress=True
        if not progress:raise ValueError('模型连接树不完整')
    return out

def link_floor(name,frame):
    v=model()['links'][name];m=mul(frame,origin(v['xyz'],v['rpy']))
    return min(sum(m[2][i]*point[i] for i in range(3))+m[2][3] for point in itertools.product(*zip(v['min'],v['max'])))
@functools.lru_cache(1)
def ground():
    fs=frames({})
    return min(link_floor(n,fs[n]) for n in fs if n.startswith('chassis_') and n.endswith('link2'))
def check_ground_path(actual,targets):
    """Reject conservative torso/head mesh boxes below fixed wheel ground.

    Samples joint interpolation every <=1 degree. This is not continuous
    collision detection and doesn't validate arm/body, balance or obstacles.
    """
    needed=set(model()['joint_map'])
    if not needed.issubset(actual):raise ValueError('地面检查缺少完整22轴反馈')
    names=[n for n in model()['links'] if n.startswith(('body_Link','head_Link'))]
    steps=max(1,math.ceil(max(abs(v-actual[n]) for n,v in targets.items())))
    floor=ground();worst=1e9;bad=None
    for i in range(steps+1):
        pose={**actual,**{n:actual[n]+(v-actual[n])*i/steps for n,v in targets.items()}};fs=frames(pose)
        for name in names:
            z=link_floor(name,fs[name]);worst=min(worst,z-floor)
            if z<floor-.005:bad=name;break
        if bad:raise ValueError(f'目标或插补路径的躯干模型穿过地面（{bad}）；请先调整姿态。此检查不是完整碰撞规划')
    return {'ground_clearance_m':worst,'samples':steps+1,'scope':'torso_head_aabb_ground_only'}
