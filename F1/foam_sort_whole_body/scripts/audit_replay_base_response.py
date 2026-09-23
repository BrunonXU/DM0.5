"""Offline per-leg response audit. Never adjusts labels, controls a robot or grants acceptance."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np


def integral(times, values, start, stop, max_gap):
    t=np.asarray(times,dtype=float); v=np.asarray(values,dtype=float)
    if len(t)<2 or v.shape!=(len(t),3) or not np.isfinite(t).all() or not np.isfinite(v).all():
        raise ValueError('invalid feedback array')
    if np.any(np.diff(t)<=0) or not t[0]<=start<stop<=t[-1]:
        raise ValueError('unordered or uncovered feedback; no extrapolation')
    lo=max(0,np.searchsorted(t,start,side='right')-1)
    hi=min(len(t)-1,np.searchsorted(t,stop,side='left'))
    if np.max(np.diff(t[lo:hi+1]),initial=0)>max_gap:
        raise ValueError('feedback gap exceeds configured freshness bound')
    inside=(t>start)&(t<stop); tt=np.r_[start,t[inside],stop]
    vv=np.vstack(([np.interp(start,t,v[:,j]) for j in range(3)],v[inside],
                  [np.interp(stop,t,v[:,j]) for j in range(3)]))
    return np.sum((vv[1:]+vv[:-1])*np.diff(tt)[:,None]/2,axis=0)


def audit(events, phase, detection_speed):
    if not np.isfinite(detection_speed) or detection_speed<=0:raise ValueError('positive onset detection speed required')
    start=next(e for e in events if e['event']=='run_start')
    if start['source_manifest']['source']['episode']!=phase['episode']:
        raise ValueError('phase and replay episode mismatch')
    commands=[e for e in events if e['event']=='command']; feedback=[e for e in events if e['event']=='feedback']
    if not commands or len(commands)!=len(feedback):raise ValueError('incomplete command/feedback pairing')
    if [c['index'] for c in commands]!=list(range(len(commands))):raise ValueError('non-contiguous source frames')
    if not any(e['event']=='source_timeline_complete' for e in events):raise ValueError('source timeline incomplete')
    n=start['source_manifest']['frames']['count']
    if n!=len(commands):raise ValueError('manifest frame count mismatch')
    origin=int(phase['source_origin_ns']);first=commands[0]
    source_offset=(int(first['source_time_ns'])-origin)/1e9
    clock0=int(first['scheduled_monotonic_ns'])
    end=source_offset+start['source_manifest']['duration_ns']/1e9
    ct=np.array([(c['dispatch_monotonic_ns']-clock0)/1e9+source_offset for c in commands])
    cv=np.array([c['command_action'][16:19] for c in commands],dtype=float)
    if cv.shape!=(len(commands),3) or not np.isfinite(cv).all() or np.any(np.diff(ct)<=0) or ct[-1]>=end:
        raise ValueError('invalid command values or dispatch time order')
    bt=np.array([(e['snapshot']['base']['received_ns']-clock0)/1e9+source_offset for e in feedback])
    bv=np.array([[e['snapshot']['base'][k] for k in ('vx','vy','wz')] for e in feedback])
    if not np.isfinite(bv).all() or np.any(np.diff(bt)<0):raise ValueError('nonfinite or backward feedback time')
    # A held snapshot must not be counted as a new sensor sample.
    for i in np.flatnonzero(np.diff(bt)==0):
        if not np.array_equal(bv[i],bv[i+1]):raise ValueError('same feedback timestamp has different values')
    bt,ids=np.unique(bt,return_index=True);bv=bv[ids]
    max_gap=float(start['profile']['feedback_max_age_s'])
    if not np.isfinite(max_gap) or max_gap<=0:raise ValueError('invalid freshness bound')
    segments=sorted((s for s in phase['segments'] if s['kind']=='motion'),key=lambda s:s['start_s'])
    rows=[]
    for i,s in enumerate(segments):
        a,b=float(s['start_s']),float(s['end_s']);d=np.array([s['dx_m'],s['dy_m'],s['dyaw_rad']],dtype=float)
        if not np.isfinite(d).all() or not source_offset<=a<b<=end or (i and a<segments[i-1]['end_s']):
            raise ValueError('phase outside replay or overlaps another movement')
        if np.count_nonzero(d)!=1 or d[2]!=0:raise ValueError('audit currently requires non-rotating single-axis route legs')
        axis=int(np.flatnonzero(d)[0]);T=b-a;settle_requested=segments[i+1]['start_s'] if i+1<len(segments) else end
        if settle_requested<b:raise ValueError('overlapping settling interval')
        # Never extend a missing final feedback sample by assuming stopped.
        settle=min(float(settle_requested),float(bt[-1]))
        window=integral(bt,bv,a,b,max_gap);full=integral(bt,bv,a,settle,max_gap)
        overlap=np.maximum(0,np.minimum(np.r_[ct[1:],end],b)-np.maximum(ct,a))
        sent=np.sum(cv*overlap[:,None],axis=0)
        within=(bt>=a)&(bt<=settle);signed=bv[:,axis]*np.sign(d[axis])
        # Three distinct sensor observations: onset diagnostic, not motion-boundary relabeling.
        moving=np.flatnonzero(within&(signed>=detection_speed));onset=None
        for j in moving:
            if j+2<len(bt) and within[j:j+3].all() and (signed[j:j+3]>=detection_speed).all() and max(np.diff(bt[j:j+3]))<=max_gap:
                onset=float(bt[j]-a);break
        rows.append({'id':s['id'],'axis':('vx','vy','wz')[axis],
            'start_s':a,'stop_s':b,'duration_s':T,'distance_prior_m':float(d[axis]),
            'peak_required_m_s':float(1.5*abs(d[axis])/T),
            'peak_acceleration_required_m_s2':float(6*abs(d[axis])/T**2),
            'command_integral_m':float(sent[axis]),'feedback_motion_window_m':float(window[axis]),
            'feedback_including_pause_m':float(full[axis]),'feedback_to_prior_ratio':float(full[axis]/d[axis]),
            'estimated_shortfall_m':float(abs(d[axis])-np.sign(d[axis])*full[axis]),
            'onset_detection_speed_m_s':detection_speed,'onset_delay_estimate_s':onset,
            'settling_stop_s':settle,'settling_requested_stop_s':settle_requested,
            'settling_truncated_s':float(settle_requested-settle)})
    return {'schema':'f1_base_response_audit_v1','episode':phase['episode'],
            'frames':len(commands),'feedback_unique_samples':len(bt),'phase_count':len(rows),'segments':rows,
            'feedback_basis':'logged latest samples at dispatch, integrated by actual receive timestamps',
            'distance_basis':'signed body-axis velocity integrals; not independently measured world displacement',
            'onset_basis':'three consecutive samples above explicitly supplied diagnostic threshold; no acceptance or label changes',
            'controller_cause_verified':False,'distance_calibration_verified':False,'training_approved':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--events',type=Path,required=True);p.add_argument('--phases',type=Path,required=True)
    p.add_argument('--onset-speed',type=float,required=True,help='m/s, diagnostic only; not a controller setting')
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    events=[json.loads(l) for l in args.events.read_text().splitlines()]
    report=audit(events,json.loads(args.phases.read_text()),args.onset_speed)
    report['sources']={name:{'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for name,path in [('events',args.events),('phases',args.phases)]}
    with args.output.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')
    print(json.dumps({'episode':report['episode'],'legs':len(report['segments']),'output':str(args.output),'distance_calibration_verified':False},ensure_ascii=False))

if __name__=='__main__':main()
