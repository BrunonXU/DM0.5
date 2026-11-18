#!/usr/bin/env python3
"""Launch only Harness auxiliary services; owner, Enter and START remain onsite."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

if __name__ == '__main__':
    # A frozen coordinator can share the development venv. Load its own package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from f1_harness.auxiliary_services import (
    # Keep the existing imports used by launcher clients and port diagnostics.
    REMOTE_GUARD as REMOTE_GUARD,
    commands,
    ready,
    require_free_ports,
    required_ports,
    socket as socket,
    stop_owned,
)
from f1_harness.site import _safe_environment


def main(argv=None):
    root=Path(__file__).resolve().parents[1]
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('local-site','remote-site','remote-bundle'):p.add_argument('--'+name,required=True)
    p.add_argument('--run',type=Path);p.add_argument('--print-only',action='store_true')
    p.add_argument('--host',default='user@127.0.0.1');p.add_argument('--port',type=int,default=22009)
    p.add_argument('--identity',default='/home/user/.ssh/id_ed25519_f1')
    p.add_argument('--known-hosts',default=str(root/'.cache/robot_known_hosts'))
    args=p.parse_args(argv);run=args.run or root/'runs'/('start-'+time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
    rows,instructions=commands(args,root,run)
    if args.print_only:print(json.dumps(dict(commands=rows,**instructions),ensure_ascii=False,indent=2));return 0
    if run.exists():raise FileExistsError('new_launcher_run_required')
    require_free_ports(required_ports(rows));run.mkdir(parents=True)
    receipt=dict(commands=rows,**instructions,owner_started=False,planner_started=False,motion_authorized=False)
    (run/'launch.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    children=[];previous=signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for row in rows:
            path=run/(row['name']+'.log');log=path.open('xb')
            try:process=subprocess.Popen(row['argv'],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,
                env=_safe_environment(),start_new_session=True)
            except BaseException:log.close();raise
            row['pid']=process.pid;children.append((row,process,log));deadline=time.monotonic()+60
            while not ready(row,path):
                if any(child.poll() is not None for _,child,_ in children):raise RuntimeError('auxiliary_process_exited:'+row['name'])
                if time.monotonic()>deadline:raise TimeoutError('auxiliary_ready_timeout:'+row['name'])
                time.sleep(.1)
        print('辅助服务就绪。现场终端运行 owner，按 Enter；另一个6000D终端启动 baseline，看到等待START后按真实手柄START。',flush=True)
        print(json.dumps(instructions,ensure_ascii=False,indent=2),flush=True)
        print('结束顺序：先停止 planner 和现场 owner，再 Ctrl+C 本辅助组。日志：'+str(run),flush=True)
        while True:
            if any(child.poll() is not None for _,child,_ in children):raise RuntimeError('auxiliary_process_exited')
            time.sleep(.2)
    except KeyboardInterrupt:receipt['state']='stopped'
    except BaseException as exc:receipt.update(state='failed',error=type(exc).__name__,reason=str(exc));raise
    finally:
        stop_owned(children);signal.signal(signal.SIGTERM,previous)
        (run/'result.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
