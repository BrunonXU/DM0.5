"""Post-commit derived jobs and causal intervention windows; raw stays immutable."""
import hashlib
import json
from pathlib import Path
import time

RENDER_VERSION='f1-three-view-v1'

def enqueue_session(run, queue):
    run,queue=Path(run),Path(queue)
    raw=(run/'journal/manifest.json').read_bytes();manifest=json.loads(raw)
    binding=manifest.get('binding',{})
    if not manifest.get('complete') or not binding.get('auto_video') or binding.get('purpose') in ('input_check','teleop_play'):return None
    if not json.loads((run/'payload/manifest.json').read_text()).get('complete'):return None
    digest=hashlib.sha256(raw).hexdigest();key=hashlib.sha256((digest+RENDER_VERSION).encode()).hexdigest()
    queue.mkdir(parents=True,exist_ok=True);path=queue/(key+'.json')
    value=dict(schema='f1_video_job_v1',id=key,run_id=run.name,source_sha256=digest,
               renderer=RENDER_VERSION,status='waiting_server',review='pending',clips=[],
               created_unix=time.time(),training_ready=False)
    try:
        with path.open('x') as stream:json.dump(value,stream)
    except FileExistsError:pass
    return key


def intervention_windows(observations, controls, purpose):
    if not observations:return []
    first=observations[0]['available_monotonic_ns'];last=observations[-1]['available_monotonic_ns']
    starts={}
    for row in controls:
        if row.get('hil_state')=='hil_latched' and row.get('intervention_id',0)>0:
            starts.setdefault(row['intervention_id'],row['monotonic_ns'])
    windows=[]
    for ident,start in starts.items():
        after=[r for r in controls if r['monotonic_ns']>=start]
        # Stop manual interval at its first departure; do not absorb a later intervention.
        departure=next((r for r in after if r.get('intervention_id')!=ident or r.get('hil_state')!='hil_latched'),None)
        resume=None;abnormal=False;failure=None
        if departure:
            for row in after:
                if row['monotonic_ns']<departure['monotonic_ns']:continue
                if row.get('hil_state')=='policy' and row.get('source')=='policy':resume=row['monotonic_ns'];break
                if row.get('hil_state')=='safe_hold' or (row.get('hil_state')=='hil_latched' and row.get('intervention_id')!=ident):abnormal=True;failure=row['monotonic_ns'];break
        cutoff=(resume+5_000_000_000 if resume else failure if abnormal else last)
        end=min(last,cutoff)
        windows.append(dict(intervention_id=ident,kind='demonstration' if purpose=='demonstration' else 'correction',
                            start_ns=max(first,start-5_000_000_000),takeover_ns=start,
                            handoff_ns=None if departure is None else departure['monotonic_ns'],
                            resume_ns=resume,end_ns=end,
                            truncated_before=start-5_000_000_000<first,
                            truncated_after=resume is None or resume+5_000_000_000>last,
                            abnormal_end=abnormal,review='pending',training_ready=False))
    return [w for w in windows if w['start_ns']<=w['end_ns']]
