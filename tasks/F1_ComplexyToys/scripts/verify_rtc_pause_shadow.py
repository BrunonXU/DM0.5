"""Inject one late reply in the actual RTC CLI, exclusively in zero-output mode."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--child',action='store_true')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.child:
        from f1dagger.rtc_policy import RTCPolicy
        from f1dagger.cli import main as cli
        original=RTCPolicy.infer_rtc
        injected=[False]
        def delayed(self,obs,rid,epoch,context):
            result=original(self,obs,rid,epoch,context)
            if not context['bootstrap'] and not injected[0]:
                injected[0]=True
                time.sleep(.8)
            return result
        RTCPolicy.infer_rtc=delayed
        runtime='/home/user/Bruno/DAgger-runtime/f1-toys-20261016'
        sys.argv=['f1dagger','sync-shadow','--rtc','--rtc-delay-steps','20','--rtc-min-steps','20',
            '--duration-seconds','12','--purpose','policy_hil','--video','off','--recording-mode','hil-record',
            '--config',runtime+'/config-rtc-5000-v4.json','--runtime',runtime+'/rtc-pause-shadow-v4',
            '--data-root','/home/user/Bruno/data/hil_data',
            '--task','Use the right gripper to pick up the wooden block and insert it into the matching opening of the container.']
        assert '--execute' not in sys.argv and sys.argv[1]=='sync-shadow'
        return cli()
    a.output.mkdir(parents=True,exist_ok=False)
    command=[sys.executable,str(Path(__file__).resolve()),'--child','--output',str(a.output)]
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        text=True,bufsize=1)
    resumed=False;run=None
    with (a.output/'console.log').open('x') as log:
        for line in proc.stdout:
            log.write(line);log.flush()
            if 'standalone sync:' in line and 'raw=' in line:run=Path(line.split('raw=',1)[1].strip())
            if 'RTC响应超时或计划耗尽' in line and not resumed:
                proc.stdin.write('\n');proc.stdin.flush();resumed=True
    code=proc.wait();proc.stdin.close()
    assert code==0 and resumed and run, (code,resumed,run)
    journal=run/'journal'
    events=[json.loads(x) for x in (journal/'events.jsonl').open()]
    paused=[e for e in events if e['event']=='rtc_timing_paused'];assert len(paused)==1
    resume=[e for e in events if e['event']=='rtc_local_resume'];assert len(resume)==1
    stale=[e for e in events if e['event']=='stale_policy_result_discarded'];assert stale
    adopted=[e for e in events if e['event']=='rtc_plan_adopted' and e['monotonic_ns']>resume[0]['monotonic_ns']]
    assert adopted and adopted[0]['context']['bootstrap']
    assert not any(e['event']=='rtc_plan_adopted' and e['request_id']==paused[0]['request_id'] for e in events)
    controls=[json.loads(x) for x in (journal/'control.jsonl').open()]
    assert all(e['execution_mode']=='shadow' and not e['publication_complete'] and not e['publications'] and e['submitted'] is None for e in controls)
    assert events[-1]['event']=='session_termination' and events[-1]['status']=='completed'
    result=dict(run=str(run),injected_delay_ms=800,paused=1,onsite_enter_simulated_only_in_shadow=True,
        late_request_discarded=True,new_bootstrap_after_resume=True,normal_journal_completion=True,
        control_records=len(controls),robot_commands=0)
    (a.output/'report.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':
    raise SystemExit(main())
