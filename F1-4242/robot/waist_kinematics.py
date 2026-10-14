"""Engineering-model planar waist FK/IK; relative height, not calibrated cm.

Parameters come from the bound URDF's first two segment origins. No borrowed
4F offsets. Other body/head axes are held. The Cartesian curve is sampled at
200Hz by the existing body stream, with all-axis speed/acceleration limits.
"""
import math
from .geometry import model
from .motion_plan import SmoothBodyPlan,MotionBudgetExceeded
NAMES=('body_j3','body_j4','body_j5')
def parameters():
    js={j['name']:j for j in model()['joints']}
    a=js['body_joint2']['xyz'];b=js['body_joint3']['xyz']
    return math.hypot(a[0],a[2]),math.hypot(b[0],b[2]),math.atan2(a[2],a[0]),math.atan2(b[2],b[0])
def forward(q):
    l1,l2,a,b=parameters();q3,q4,q5=[math.radians(q[n]) for n in NAMES]
    return l1*math.cos(a+q5)+l2*math.cos(b+q5-q4),l1*math.sin(a+q5)+l2*math.sin(b+q5-q4),q3-q4+q5

def inverse(x,z,pitch,seed,limits):
    l1,l2,a,b=parameters();c=(x*x+z*z-l1*l1-l2*l2)/(2*l1*l2)
    if not -1<=c<=1:raise ValueError('保持俯仰时已到模型升降工作空间边界；需要改变姿态')
    candidates=[]
    for e in (-math.acos(c),math.acos(c)):
        q5=math.atan2(z,x)-math.atan2(l2*math.sin(e),l1+l2*math.cos(e))-a
        for k in (-1,0,1):
            q5k=q5+2*k*math.pi;q4=b-a-e;q3=pitch+q4-q5k
            row=dict(zip(NAMES,map(math.degrees,(q3,q4,q5k))))
            if all(limits[n][0]-1e-7<=row[n]<=limits[n][1]+1e-7 for n in NAMES):candidates.append(row)
    if not candidates:raise ValueError('保持俯仰的升降目标超出关节行程；请减小位移或改变弯腰姿态')
    return min(candidates,key=lambda row:sum((row[n]-seed[n])**2 for n in NAMES))

class CartesianBodyPlan(SmoothBodyPlan):
    def __init__(self,origin,delta_m,limits,speed,acceleration,max_motion_s=5.7):
        if isinstance(delta_m,bool) or not isinstance(delta_m,(float,int)) or not math.isfinite(delta_m) or abs(delta_m)>.1:raise ValueError('本次相对升降位移须在 -10 至 10 cm')
        self.origin=dict(origin);self.delta_m=delta_m;self.limits=limits;self.x,self.z,self.pitch=forward(origin)
        self.targets={**origin,**inverse(self.x,self.z+delta_m,self.pitch,origin,limits)}
        # Dense normalized quintic trajectory, with a 5% numerical derivative
        # margin; each dispatch also stays within the bound model limits.
        self.duration=1.;steps=500;rows=[self.sample(i/steps) for i in range(steps+1)]
        self.duration=.1
        for n in NAMES:
            velocities=[(b[n]-a[n])*steps for a,b in zip(rows,rows[1:])]
            accelerations=[(b-a)*steps for a,b in zip(velocities,velocities[1:])]
            self.duration=max(self.duration,1.05*max(map(abs,velocities))/speed,math.sqrt(1.05*max(map(abs,accelerations))/acceleration))
        if self.duration>max_motion_s:raise MotionBudgetExceeded(self.duration,max_motion_s)
    def sample(self,elapsed):
        f=self.fraction(elapsed)
        return {**self.origin,**inverse(self.x,self.z+self.delta_m*f,self.pitch,self.origin,self.limits)}
