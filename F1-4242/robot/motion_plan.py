"""Fixed configured speeds, acceleration ramps and a six-second action budget."""
import math

class MotionBudgetExceeded(ValueError):
    def __init__(self, duration, maximum):
        self.motion_s=duration;self.max_motion_s=maximum
        message=(f'固定速度下预计运动 {duration:.2f} 秒，超过 6 秒预算；请拆分目标，不会自动提速' if maximum<=5.7 else
                 f'固定速度下预计运动 {duration:.2f} 秒，超过整套任务时限 {maximum+.3:g} 秒；请调整目标，不会自动提速')
        super().__init__(message)


class FixedPlan:
    def __init__(self,origin,targets,speeds,accelerations,max_motion_s=5.7):
        self.origin=dict(origin);self.targets=dict(targets)
        distances={n:abs(targets[n]-origin[n]) for n in targets if abs(targets[n]-origin[n])>1e-9}
        self.distance=max(distances.values(),default=0.)
        if not distances:
            self.acceleration=1.;self.peak=0.;self.ramp=0.;self.cruise=0.;self.duration=.1;return
        rate=min(speeds[n]/d for n,d in distances.items());acc=min(accelerations[n]/d for n,d in distances.items())
        if not math.isfinite(rate) or not math.isfinite(acc) or rate<=0 or acc<=0:raise ValueError('固定速度配置无效')
        self.acceleration=acc;self.peak=min(rate,math.sqrt(acc));self.ramp=self.peak/acc
        self.cruise=max(0.,(1-self.peak*self.ramp)/self.peak);self.duration=2*self.ramp+self.cruise
        if self.duration>max_motion_s:raise MotionBudgetExceeded(self.duration,max_motion_s)
    def fraction(self,elapsed):
        if self.distance==0:return 1.
        t=max(0.,min(elapsed,self.duration))
        if t<self.ramp:return .5*self.acceleration*t*t
        if t<self.ramp+self.cruise:return .5*self.peak*self.ramp+self.peak*(t-self.ramp)
        return 1-.5*self.acceleration*(self.duration-t)**2
    def sample(self,elapsed):
        f=self.fraction(elapsed)
        return {n:self.origin[n]+(value-self.origin[n])*f for n,value in self.targets.items()}

# Hall's unique configured lift postures. Angles, not absolute height calibration.
LIFT_PRESETS=(('低位',(-5.,-10.,-5.)),('中低位',(-10.,-20.,-10.)),('中高位',(-15.,-30.,-15.)),('高位',(-20.,-40.,-20.)))

def lift_targets(level):
    if type(level) is not int or not 1<=level<=len(LIFT_PRESETS):raise ValueError('升降档位应为 1–4')
    return dict(zip(('body_j3','body_j4','body_j5'),LIFT_PRESETS[level-1][1]))

class SmoothBodyPlan(FixedPlan):
    """Synchronized quintic position; zero endpoint velocity AND acceleration.

    T obeys exact normalized quintic peaks (v=1.875, a=10/sqrt(3)).
    This is joint-space interpolation, not a Cartesian height controller.
    """
    def __init__(self,origin,targets,speeds,accelerations,max_motion_s=5.7):
        self.origin=dict(origin);self.targets=dict(targets)
        self.duration=.1
        for name,target in targets.items():
            d=abs(target-origin[name]);v=speeds[name];a=accelerations[name]
            if not all(math.isfinite(x) and x>0 for x in (v,a)):raise ValueError('固定速度配置无效')
            self.duration=max(self.duration,1.875*d/v,math.sqrt(10/math.sqrt(3)*d/a))
        if self.duration>max_motion_s:raise MotionBudgetExceeded(self.duration,max_motion_s)
    def fraction(self,elapsed):
        t=max(0.,min(1.,elapsed/self.duration))
        return 10*t**3-15*t**4+6*t**5

class BodyCadence:
    """Absolute deadlines, no catch-up burst; counts dispatch gaps, not DDS delivery."""
    def __init__(self,start,hz,max_gap_s):
        if not 0<hz<=1000 or max_gap_s<1/hz:raise ValueError('身体下发节拍配置无效')
        self.start=start;self.period=1/hz;self.max_gap_s=max_gap_s;self.times=[]
    def dispatch(self,now):
        if self.times and now-self.times[-1]>self.max_gap_s:
            raise ValueError(f'身体下发间隔 {(now-self.times[-1])*1000:.1f}ms 超出节拍预算，已停止；不会跳帧追赶目标')
        self.times.append(now)
    def wait_s(self,now):
        deadline=self.start+(math.floor((now-self.start)/self.period+1e-8)+1)*self.period
        return max(0.,deadline-now)
    def report(self):
        gaps=sorted(b-a for a,b in zip(self.times,self.times[1:]))
        return {'target_hz':1/self.period,'samples':len(self.times),
                'actual_hz':(len(self.times)-1)/(self.times[-1]-self.times[0]) if len(self.times)>1 and self.times[-1]>self.times[0] else None,
                'p95_gap_ms':gaps[min(len(gaps)-1,int(len(gaps)*.95))]*1000 if gaps else None,
                'max_gap_ms':max(gaps)*1000 if gaps else None,
                'scope':'application_dispatch_not_motor_servo_or_dds_delivery'}
