"""Opt-in integration of prefix recovery with the existing sole output owner.

No default CLI imports this module. An isolated experimental entry must select
both this ControlLoop and RTCFeasiblePrefixEngine explicitly.
"""
import os
import numpy as np

from .runtime import ControlLoop
from .rtc_prefix_recovery import RTCFeasiblePrefixEngine, PrefixStopEvidence


class PrefixRecoveryControlLoop(ControlLoop):
    def __init__(self, engine, owner, arm, recorder, profile, **kwargs):
        super().__init__(engine, owner, arm, recorder, profile, **kwargs)
        if not isinstance(engine, RTCFeasiblePrefixEngine):
            raise TypeError('Prefix recovery requires its explicit experimental engine')
        planner=owner.output.planner
        if getattr(planner,'internal_units',None)!='radian' or getattr(planner,'bounds',None) is None:
            raise ValueError('Prefix recovery requires existing bounded RadianArmTrajectory')
        values=[float(profile[k]) for k in ('joint_tolerance_deg','feedback_max_age_s','settle_timeout_s')]
        if not np.isfinite(values).all() or min(values)<=0 or not np.isfinite(planner.deadband) or planner.deadband<=0:
            raise ValueError('Invalid existing profile arrival/feedback/settle/deadband')
        self._stationary_publication=None
        self._stationary_failure='not_yet_captured'
        engine.prefix_owner_token=f'prefix-owner:{os.getpid()}:{id(owner)}'
        engine.prefix_stop_tolerance_deg=values[0]
        engine.prefix_feedback_max_age_ns=round(values[1]*1e9)
        engine.prefix_settle_timeout_s=values[2]
        engine.prefix_planner_deadband_rad=float(np.deg2rad(planner.deadband))
        engine.prefix_stop_probe=self.stop_probe
        engine.prefix_stop_diagnostics=self.stop_diagnostics
        engine.feasible_prefix_enabled=True
        engine.emit('rtc_prefix_recovery_configured',owner_token=engine.prefix_owner_token,
            powered=owner.powered,arrival_tolerance_deg=values[0],feedback_max_age_s=values[1],
            settle_timeout_s=values[2],planner_deadband_deg=planner.deadband,
            tracking_hold_recovery_enabled=engine.tracking_hold_recovery_enabled,
            limits_changed=False,gripper_values_preserved=True,
            shadow_does_not_certify_physical_stopping=True)

    def step(self,*args,**kwargs):
        super().step(*args,**kwargs)
        self.capture_stationary_publication()

    def capture_stationary_publication(self):
        recovery=self.engine.prefix_recovery or self.engine.tracking_hold_recovery
        owner=self.owner
        if recovery is None or recovery.get('request') is None or not owner.powered:
            self._stationary_publication=None
            self._stationary_failure=('no_recovery' if recovery is None else
                'shadow_no_physical_evidence' if not owner.powered else 'awaiting_measured_hold_publication')
            return
        request=recovery['request'];evidence=owner.last_evidence;planner=owner.output.planner
        certificate=planner._certificate
        if (evidence.get('publication_complete') is not True or evidence.get('execution_mode')!='powered'
                or evidence.get('completed_monotonic_ns',0)<request.endpoint_monotonic_ns
                or not isinstance(certificate,tuple) or len(certificate)!=2):
            self._stationary_publication=None
            self._stationary_failure='missing_completed_publication_or_certificate'
            return
        trajectory,elapsed=certificate
        if elapsed<trajectory.duration:
            self._stationary_publication=None
            self._stationary_failure='planner_trajectory_in_progress'
            return
        native=planner._radians
        pos=np.array(native.position[7:14]);vel=np.array(native.velocity[7:14]);acc=np.array(native.acceleration[7:14])
        terminal=np.array(trajectory.at_time(trajectory.duration)[0][7:14])
        target=np.array(request.target16);published=np.asarray(owner.last_submitted)
        tol=self.engine.prefix_planner_deadband_rad
        if (not np.isfinite(np.r_[pos,vel,acc,terminal,published]).all()
                or np.max(abs(vel))>1e-9 or np.max(abs(acc))>1e-9
                or not np.allclose(pos,terminal,atol=tol,rtol=0)
                or not np.allclose(terminal,np.deg2rad(target[7:14]),atol=tol,rtol=0)
                or not np.allclose(np.deg2rad(published[7:14]),pos,atol=tol,rtol=0)
                or not np.array_equal(published[14:],target[14:])):
            self._stationary_publication=None
            self._stationary_failure='planner_motion_or_terminal_publication_mismatch'
            return
        identity=(request.policy_epoch,request.request_id,request.owner_token,request.target16)
        self._stationary_failure=None
        old=self._stationary_publication
        if old is not None and old['identity']==identity and np.allclose(old['pos'],pos,atol=tol,rtol=0):
            # Keep the first actual stationary dispatch so slower feedback can
            # advance beyond it. Do not invent a publication at the latest tick.
            return
        self._stationary_publication=dict(identity=identity,pos=pos.copy(),vel=vel.copy(),acc=acc.copy(),
            terminal=terminal.copy(),published=published.copy(),time_ns=evidence['completed_monotonic_ns'])

    def stop_diagnostics(self,request,now):
        """Read-only explanation; never authorizes motion or replaces stop proof."""
        snap=self._stationary_publication
        identity=(request.policy_epoch,request.request_id,request.owner_token,request.target16)
        report=dict(powered=self.owner.powered,stationary_publication_available=snap is not None,
            stationary_publication_blocker=self._stationary_failure,
            same_recovery_identity=snap is not None and snap['identity']==identity,
            arrival_tolerance_deg=self.engine.prefix_stop_tolerance_deg,
            observed_monotonic_ns=now,endpoint_monotonic_ns=request.endpoint_monotonic_ns)
        if not self.owner.powered:
            report['blockers']=['shadow_no_physical_evidence']
            return report
        _,measured,stamps=self.owner.source.feedback()
        measured=np.asarray(measured,float);stamps=np.asarray(stamps)
        if (measured.shape!=(16,) or not np.isfinite(measured).all() or stamps.shape!=(16,)
                or not np.issubdtype(stamps.dtype,np.integer)):
            report['blockers']=['invalid_feedback']
            return report
        target=np.asarray(request.target16)[7:14]
        errors=np.abs(measured[7:14]-target)
        fresh=bool(np.all(stamps[7:14]<=now) and np.all(now-stamps[7:14]<=self.engine.prefix_feedback_max_age_ns))
        after=bool(snap is not None and np.all(stamps[7:14]>snap['time_ns']))
        failed=(np.flatnonzero(errors>self.engine.prefix_stop_tolerance_deg)+1).tolist()
        report.update(target_right_deg=target.tolist(),measured_right_deg=measured[7:14].tolist(),
            absolute_error_right_deg=errors.tolist(),max_error_joint=int(np.argmax(errors))+1,
            max_error_deg=float(errors.max()),joints_outside_arrival_tolerance=failed,
            feedback_age_ms=((now-stamps[7:14])/1e6).tolist(),feedback_fresh=fresh,
            feedback_after_stationary_publication=after,
            stationary_publication_monotonic_ns=None if snap is None else snap['time_ns'])
        report['blockers']=[]
        if not report['same_recovery_identity']:
            report['blockers'].append(self._stationary_failure or 'stationary_identity_mismatch')
        if not fresh:report['blockers'].append('feedback_stale_or_future')
        if snap is not None and not after:report['blockers'].append('feedback_precedes_stationary_publication')
        if failed:report['blockers'].append('measured_target_not_reached')
        return report

    def stop_probe(self,request,now):
        snap=self._stationary_publication
        identity=(request.policy_epoch,request.request_id,request.owner_token,request.target16)
        if snap is None or snap['identity']!=identity or not self.owner.powered:
            return None
        _,measured,stamps=self.owner.source.feedback()
        if any(t>now for t in stamps[7:14]):
            return None
        return PrefixStopEvidence(policy_epoch=request.policy_epoch,request_id=request.request_id,
            owner_token=request.owner_token,target16=request.target16,certificate_valid=True,
            certified_right_rad=tuple(snap['terminal']),planner_right_position_rad=tuple(snap['pos']),
            planner_right_velocity_rad_s=tuple(snap['vel']),planner_right_acceleration_rad_s2=tuple(snap['acc']),
            published16=tuple(snap['published']),published_monotonic_ns=snap['time_ns'],
            measured16=tuple(measured),feedback_timestamps_ns=tuple(stamps),observed_monotonic_ns=now)
