"""Independent r3 experiment; original entry and default engine stay unchanged."""
from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine
from f1dagger.rtc_prefix_runtime import PrefixRecoveryControlLoop
from f1dagger import rtc, runtime, cli


class RTCTrackingHoldRecoveryEngine(RTCFeasiblePrefixEngine):
    def __init__(self,*args,**kwargs):
        kwargs['tracking_hold_recovery_enabled']=True
        super().__init__(*args,**kwargs)


rtc.RTCPolicyEngine=RTCTrackingHoldRecoveryEngine
runtime.ControlLoop=PrefixRecoveryControlLoop

if __name__=='__main__':
    cli.main()
