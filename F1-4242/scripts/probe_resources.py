#!/usr/bin/env python3
"""Read-only resource sample of the 4242 service cgroup; never starts workloads."""
import argparse,json,os,subprocess,time,urllib.request
from pathlib import Path
UNIT='f1-console-4242.service'

def snapshot():
    keys=('MainPID','ActiveState','ControlGroup','CPUUsageNSec','MemoryCurrent','IOReadBytes','IOWriteBytes','TasksCurrent')
    cmd=['systemctl','--user','show',UNIT]
    for key in keys:cmd.extend(['-p',key])
    fields=dict(line.split('=',1) for line in subprocess.check_output(cmd,text=True).splitlines() if '=' in line)
    for key in keys:
        if fields.get(key,'').isdigit():fields[key]=int(fields[key])
    rss=0;pids=[];cg=Path('/sys/fs/cgroup')/fields.get('ControlGroup','').lstrip('/')
    if fields.get('ControlGroup') and cg.is_dir():
        for p in cg.rglob('cgroup.procs'):
            try:pids.extend(int(x) for x in p.read_text().split())
            except OSError:pass
        for pid in set(pids):
            try:rss+=int((Path('/proc')/str(pid)/'statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
            except (OSError,IndexError,ValueError):pass
    fields.update(process_count=len(set(pids)),rss_sum_bytes=rss,monotonic=time.monotonic())
    return fields

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--seconds',type=float,default=15);args=parser.parse_args()
    if not 1<=args.seconds<=60:parser.error('--seconds must be between 1 and 60')
    before=snapshot();time.sleep(args.seconds);after=snapshot();elapsed=after['monotonic']-before['monotonic']
    result={'unit':UNIT,'checked_unix':time.time(),'elapsed_s':elapsed,'before':before,'after':after,'cpu_percent_one_core':None,'scope':'whole service cgroup, including recorder/encoders if active; no workload started'}
    if before.get('MainPID')==after.get('MainPID') and after.get('MainPID',0)>0:
        for key,label,scale in [('CPUUsageNSec','cpu_percent_one_core',1e-7),('IOReadBytes','read_bytes_per_s',1),('IOWriteBytes','write_bytes_per_s',1)]:
            if isinstance(before.get(key),int) and isinstance(after.get(key),int) and 0<=before[key]<=after[key]<2**63:
                result[label]=(after[key]-before[key])*scale/elapsed
    else:result['invalid_reason']='service stopped or restarted during sample'
    for path,label in [('/api/state','robot_state'),('/api/recording/state','recording')]:
        try:
            with urllib.request.urlopen('http://127.0.0.1:4242'+path,timeout=4) as response:data=json.load(response)
            result[label]={k:data[k] for k in ('cameras','control','action','recording') if k in data}
        except Exception as exc:result[label]={'error':str(exc)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
