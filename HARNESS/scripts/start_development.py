#!/usr/bin/env python3
"""Launch only Harness auxiliary services; owner, Enter and START remain onsite."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
import socket
import subprocess
import time
import uuid

from f1_harness.collect import ssh_reader_command
from f1_harness.site import _safe_environment

# SSH stdin is deliberately kept open. Disconnect/EOF requests clean SIGINT on
# this one native services group; its existing supervisor reaps its own children.
REMOTE_GUARD = r'''
import os,select,signal,subprocess,sys,threading
stop=threading.Event()
for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,lambda *_:stop.set())
child=subprocess.Popen(sys.argv[1:],start_new_session=True)
try:
    while child.poll() is None and not stop.is_set():
        readable,_,_=select.select([sys.stdin.fileno()],[],[],.1)
        if readable and not os.read(sys.stdin.fileno(),4096):stop.set()
finally:
    if child.poll() is None:
        os.killpg(child.pid,signal.SIGINT)
        try:child.wait(timeout=12)
        except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
raise SystemExit(child.returncode)
'''


def commands(args, root, run):
    site=Path(args.local_site).resolve();run=Path(run).resolve();root=Path(root).resolve()
    for path,parent in ((args.remote_site,'/home/user/Bruno/HARNESS/site'),
                        (args.remote_bundle,'/home/user/Bruno/HARNESS/development')):
        value=PurePosixPath(path)
        if not value.is_relative_to(parent) or '..' in value.parts or str(value)==parent:
            raise ValueError('explicit_harness_remote_path_required')
    gateway=json.loads((site/'gateway.local.json').read_text());masks=json.loads((site/'masks.local.json').read_text())
    mask_execution=gateway.get('scene',{}).get('mask_execution','remote')
    if mask_execution not in ('remote','local_fixed_color','local_sam2'):
        raise ValueError('unknown_mask_execution')
    if (gateway.get('control_port'),gateway.get('image_port'),masks.get('port'))!=(19840,19841,19842):
        raise ValueError('launcher_requires_registered_19840_19841_19842_ports')
    ssh=ssh_reader_command({},host=args.host,port=args.port,identity=args.identity,known_hosts=args.known_hosts)[:-1]
    native='/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python'
    remote_run='/home/user/Bruno/HARNESS/runs/services-'+run.name
    entry=[native,args.remote_bundle+'/native/entry.py']
    service_argv=entry+['--run',remote_run,'services',args.remote_site,'--mode','hardware']
    pool=getattr(args,'numerical_pool',None)
    if pool:service_argv+=['--numerical-pool',str(pool)]
    remote='source /opt/ros/jazzy/setup.bash\nexec '+shlex.join(service_argv)
    rows=[dict(name='masks',argv=[str(root/'perception/.venv/bin/python'),str(root/'native/entry.py'),
          '--run',str(run/'masks'),'serve','masks',str(site/'masks.local.json')]),
        dict(name='control_tunnel',port=19840,argv=ssh[:-1]+['-o','ExitOnForwardFailure=yes','-N','-L',
          '127.0.0.1:19840:127.0.0.1:19840',ssh[-1]]),
        dict(name='image_tunnel',port=19841,argv=ssh[:-1]+['-o','ExitOnForwardFailure=yes','-N','-L',
          '127.0.0.1:19841:127.0.0.1:19841','-R','127.0.0.1:19842:127.0.0.1:19842',ssh[-1]]),
        dict(name='robot_services',remote=True,argv=ssh+[shlex.join(['python3','-c',REMOTE_GUARD,'bash','-c',remote])])]
    if mask_execution in ('local_fixed_color','local_sam2'):
        rows=rows[1:]
    if mask_execution=='local_sam2':
        import urllib.request
        expected=next(iter(masks['views'].values()))['checkpoint_sha256']
        health=json.load(urllib.request.urlopen('http://127.0.0.1:19842/health',timeout=2))
        if health.get('ready') is not True or health.get('model_sha256')!=expected:
            raise ValueError('sam_service_not_ready')
    if mask_execution=='local_fixed_color':
        image=next(row for row in rows if row['name']=='image_tunnel')
        reverse=image['argv'].index('-R')
        del image['argv'][reverse:reverse+2]
    owner='source /opt/ros/jazzy/setup.bash\n'+shlex.join(entry+['--run',remote_run+'-owner','owner',args.remote_site,'--mode','hardware'])
    planner=shlex.join([str(root/'.venv/bin/python'),'-m','f1_harness.cli','--run',str(run/'planner'),
        'run-task',str(site/'task.local.json'),'--gateway','http://127.0.0.1:19840',
        '--image-gateway','http://127.0.0.1:19841','--planner','astra'])
    return rows,dict(owner_command=owner,planner_command=planner,remote_services_run=remote_run,
        windows_tunnel='ssh -F NUL -N -o ExitOnForwardFailure=yes -L 127.0.0.1:7907:127.0.0.1:7906 user@192.0.2.7',
        windows_sender='cd "$HOME/DAgger-F1"\n.\\scripts\\windows_f1_sender.cmd')


def required_ports(rows):
    return (19840,19841,19842) if any(row['name']=='masks' for row in rows) else (19840,19841)


def require_free_ports(ports=(19840,19841,19842)):
    sockets=[]
    try:
        for port in ports:
            item=socket.socket();sockets.append(item)
            # Match SSH's listener semantics. A closed previous tunnel may leave
            # accepted connections in TIME_WAIT; that is not a live port owner.
            # listen() still rejects an actually occupied endpoint. The real
            # SSH launch also retains ExitOnForwardFailure for races afterward.
            item.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            try:
                item.bind(('127.0.0.1',port))
                item.listen(1)
            except OSError as exc:raise ValueError('local_port_already_in_use:'+str(port)) from exc
    finally:
        for item in sockets:item.close()


def ready(row,log):
    if 'port' in row:
        try:
            with socket.create_connection(('127.0.0.1',row['port']),timeout=.1):return True
        except OSError:return False
    for line in log.read_text(errors='replace').splitlines():
        try:value=json.loads(line)
        except ValueError:continue
        if isinstance(value,dict) and value.get('ready') is True and (
                value.get('service')==row['name'] or row.get('remote') and 'services' in value):return True
    return False


def stop_owned(children):
    for row,process,log in reversed(children):
        if process.stdin:process.stdin.close()  # EOF lets the remote native group clean up first.
        if process.poll() is None:
            if not row.get('remote'):
                try:os.killpg(process.pid,signal.SIGINT)
                except ProcessLookupError:pass
            try:process.wait(timeout=15 if row.get('remote') else 7)
            except subprocess.TimeoutExpired:
                row['cleanup_timeout']=True
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=2)
        row['returncode']=process.returncode;log.close()


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
