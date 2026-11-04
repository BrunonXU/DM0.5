#!/usr/bin/env python3
"""Independent phase/time inequality analysis. Does not synthesize model/robot actions."""
import hashlib
import json
import math
from pathlib import Path
TASK=Path(__file__).resolve().parents[1]
OUT=TASK/'Eval/artifacts/20261021-ik-recovery-next/partial'
HZ=30.


def phase_at(t, prefix, ratio, ramp_ms):
    """Smoothstep phase velocity 1->ratio, only after immutable prefix ends."""
    boundary=prefix/HZ
    if t<=boundary:return HZ*t
    x=t-boundary;tau=ramp_ms/1000
    if x<=tau:
        z=x/tau
        return prefix+HZ*(x+(ratio-1)*tau*(2.5*z**4-3*z**5+z**6))
    return prefix+HZ*(tau*(1+ratio)/2+ratio*(x-tau))


def time_at(u,prefix,ratio,ramp_ms):
    if u<=prefix:return u/HZ
    tau=ramp_ms/1000
    a=(u-prefix)/HZ
    ramp_phase_duration=tau*(1+ratio)/2
    if a<=ramp_phase_duration:
        lo,hi=0.,tau
        for _ in range(45):
            x=(lo+hi)/2;z=x/tau
            integral=x+(ratio-1)*tau*(2.5*z**4-3*z**5+z**6)
            if integral<a:lo=x
            else:hi=x
        x=(lo+hi)/2
    else:
        x=tau+(a-ramp_phase_duration)/ratio
    return prefix/HZ+x


def opportunities(valid,prefix,adopt_ms,ratio,ramp_ms,latency_ms=400,stop_ms=66.667,dispatch_ms=33.334):
    """Try integer S,D. New observation wait33.334ms; all endpoints same timewarp.

    Deadline requires full prediction+adoption budget latency+dispatch before
    committed-prefix expiry. Emergency stop may begin when prediction returns;
    stop_ms is an explicit unverified physical bound, not safety certification.
    """
    earliest=(adopt_ms+33.334)/1000
    times=[time_at(u,prefix,ratio,ramp_ms) for u in range(valid+1)]
    last=times[valid-1]
    plans=[]
    for s in range(1,valid):
        query=times[s]
        if query<earliest:continue
        for d in range(1,min(s,49-s,valid-s)+1):
            prefix_end=times[s+d]
            reply=query+(latency_ms+dispatch_ms)/1000
            stop_deadline=last-stop_ms/1000
            if reply>=prefix_end or reply>stop_deadline:continue
            plans.append(dict(s=s,d=d,query_ms=query*1000,reply_and_adoption_ms=reply*1000,
                prefix_end_ms=prefix_end*1000,last_valid_conservative_ms=last*1000,
                stop_start_deadline_ms=stop_deadline*1000,
                reply_margin_ms=(min(prefix_end,stop_deadline)-reply)*1000,
                immutable_original_prefix_duration_ms=prefix/HZ*1000,
                phase_at_query=phase_at(query,prefix,ratio,ramp_ms),
                phase_at_reply=phase_at(reply,prefix,ratio,ramp_ms)))
    return sorted(plans,key=lambda p:(p['query_ms'],-p['reply_margin_ms'],p['d']))


def main():
    src=TASK/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
    alt=TASK/'Eval/artifacts/20261020-driver-range-study/results-j2-refinement/j2_minus97_only.json'
    rows=[json.loads(x) for x in src.read_text().splitlines()]
    replays={(r['episode'],r['request_id']):r for r in json.loads(alt.read_text())['full_replays']}
    records=[]
    for row in rows:
        raw=row['server'];c=raw['rtc_request'];rid=raw['request_id']
        if not row['episode'].endswith('3dd2babb') or rid not in range(58,64):continue
        replay=replays[(row['episode'],rid)]
        prefix=len(c['committed_actions16'])
        valid=prefix+sum(x['success'] for x in replay['diagnostics'])
        # Expansion decoding takes an additional assumed40ms; bootstrap origin
        # is adoption, guided origin is historical RTC clock and cannot be reset.
        adopt=0 if c['bootstrap'] else (row['client_timing']['monotonic_ns']-c['plan_origin_ns'])/1e6+40
        cases=[]
        for ramp in [100,200,300]:
            for latency in [300,400,467]:
                for stop in [66.667,150,300]:
                    coarse=[]
                    for ratio in [1.,.95,.9,.85,.8,.75,.7,.65,.6,.55,.5,.45,.4,.35,.3,.25]:
                        plans=opportunities(valid,prefix,adopt,ratio,ramp,latency,stop)
                        coarse.append(dict(ratio=ratio,feasible_time_inequalities=bool(plans),
                                           first_plan=plans[0] if plans else None))
                    # 0.001 resolution scan for largest speed fraction that passes.
                    threshold=None
                    for step in range(1000,199,-1):
                        ratio=step/1000
                        plans=opportunities(valid,prefix,adopt,ratio,ramp,latency,stop)
                        if plans:
                            threshold=dict(ratio=ratio,first_plan=plans[0]);break
                    cases.append(dict(ramp_ms=ramp,next_latency_ms=latency,stop_reserve_ms=stop,
                                      dispatch_margin_ms=33.334,coarse=coarse,max_grid_speed=threshold))
        records.append(dict(episode=row['episode'],request_id=rid,bootstrap=c['bootstrap'],
            immutable_prefix_count=prefix,valid_total=valid,adopt_elapsed_ms=adopt,cases=cases))
    # Actual-prefix samples strictly preserve original timestamps and phase.
    checks={}
    checks['immutable_20_steps_unchanged']=all(abs(time_at(u,20,.4,200)-u/HZ)<1e-12 for u in range(21))
    checks['phase_time_inverse']=all(abs(phase_at(time_at(u,p,r,200),p,r,200)-u)<1e-10
        for p in [0,20] for r in [.3,.5,.8,1.] for u in [0,1,5,12,20,23,27])
    # Quintic smoothstep has zero first/second derivative at ramp endpoints.
    smooth=lambda z:10*z**3-15*z**4+6*z**5
    smooth_d=lambda z:30*z*z-60*z**3+30*z**4
    smooth_dd=lambda z:60*z-180*z*z+120*z**3
    checks['ramp_velocity_acceleration_jerk_endpoints']=(smooth(0)==0 and smooth(1)==1 and
        all(smooth_d(z)==0 and smooth_dd(z)==0 for z in [0,1]))
    checks['monotone_phase_mapping']=all(phase_at(i/1000,p,r,200)<phase_at((i+1)/1000,p,r,200)
        for p in [0,20] for r in [.3,.5,.8,1.] for i in range(1500))
    checks['no_model_or_robot_calls']=True
    assert all(checks.values())
    headline=[]
    for r in records:
        selected=[c for c in r['cases'] if c['ramp_ms']==200 and c['next_latency_ms']==400]
        headline.append(dict(request_id=r['request_id'],valid_total=r['valid_total'],
            immutable_prefix_count=r['immutable_prefix_count'],
            max_speed_at_stop_reserve={str(c['stop_reserve_ms']):c['max_grid_speed']['ratio'] for c in selected},
            passes_at_half_speed_66ms_stop=next(x['feasible_time_inequalities'] for c in selected if c['stop_reserve_ms']==66.667 for x in c['coarse'] if x['ratio']==.5)))
    output=dict(schema='partial_timewarp_inequalities_v1',date='2026-10-21',records=records,checks=checks,headline=headline,
        shortest_window_bound=dict(request_id=61,valid_total=13,
            d_upper_bound_from_d_le_s_and_s_plus_d_le_valid=6,
            maximum_prefix_wall_ms_at_half_speed=400,
            required_prediction_and_dispatch_ms=433.334,
            phase_speed_upper_bound_even_without_stop_reserve=6/(30*.433334),
            derivation='D<=S and S+D<=13 imply D<=6. Phase velocity never falls below30*r, so anyD prefix lasts at mostD/(30*r). At r>=0.5 this is<=400ms, insufficient for433.334ms prediction+adoption even before stop reserve.'),
        timing_contract={
            'phase':'u advances along original30Hz model waypoint index; original interpolated joint/EEF path is retained, wall time changes only after original immutable prefix.',
            'ramp':'du/dt=30 before prefix boundary; quintic smoothstep velocity ramp1->ratio over100/200/300ms; afterwards30*ratio. Its first/second derivatives vanish at endpoints. Bootstrap also conservatively starts phase velocity1 then ramps, so no free instant slowdown benefit.',
            'current_adoption':'Guided58 adopts at recorded validated reply+assumed40ms extra expandedIK; immutable20 points still end at666.667ms. Bootstrap origins are new adoption time.',
            'new_query':'At integer phaseS after adoption+33.334ms fresh-image wait; nextD is any integer satisfying1<=D<=S<50-D, S+D<=valid_total.',
            'next_deadline':'query_time + assumed_next_latency +33.334ms adoption/dispatch margin < timewarp(S+D). Same predicted reply must be<=timewarp(valid_total-1)-stop_reserve.',
            'origin':'Next plan clock must retain the same absolute phase-to-wall mapping over its committed prefix; a constant30Hz sampler or new unwarped deadline is invalid.',
            'path':'Stored EEF and joint target values unchanged; changing execution duration changes dynamics and observed object/contact state, so not behaviorally equivalent.'},
        caveats=[
            'Only necessary time inequalities. No new observation, candidate prediction, dynamic tracking, physical movement or closed-loop success.',
            'Phase-velocity ramp is smooth through endpoint acceleration/jerk, but inherited piecewise-linear joint waypoints and actual output tracking are not certified by it. Production needs continuous joint position/velocity/acceleration and jerk-limited transition from actual output state; these ramps are illustrative, not accepted robot trajectories.',
            'Constant scaling of joint paths preserves waypoint FK, but interpolated joints need not follow linearly interpolated Cartesian poses; this does not change the existing interpolation convention.',
            'Stopping reserve is assumed, not certified against actual velocity/acceleration, contacts or gripper state.',
            'PhaseD versus walltimeD changes RTC inference conditioning: lowerD under slowdown changes how much generated future is hard-conditioned, although no generatedEEF is manually edited. Needs model evaluation.',
            'A fixed30Hz training horizon spanning1.67 nominal seconds becomes longer in walltime; grip and insertion dynamics may change. Do not slow only the arm and leave gripper on a different clock.',
            'Production must share one timewarp across sampler, observation anchor, prefix slicing, deadline and replan origin. Changing only execution speed breaks RTC semantics.',
            'J2-97 hardware permission remains unresolved; usable lengths are candidate mathematical evidence only.',
            'This is separate from existing execution-speed work and modifies no production configuration.'],
        model_calls=0,robot_commands=0,production_changes=0)
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'timewarp-analysis.json').write_text(json.dumps(output,indent=2)+'\n')
    (OUT/'timewarp-summary.json').write_text(json.dumps({k:v for k,v in output.items() if k!='records'},indent=2)+'\n')
    (OUT/'timewarp-manifest.json').write_text(json.dumps(dict(sources=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in [src,alt,Path(__file__)]]),indent=2)+'\n')
    for r in records:
        print(r['request_id'],r['valid_total'],[(c['stop_reserve_ms'],c['max_grid_speed']['ratio'] if c['max_grid_speed'] else None) for c in r['cases'] if c['ramp_ms']==200 and c['next_latency_ms']==400])
if __name__=='__main__':main()
