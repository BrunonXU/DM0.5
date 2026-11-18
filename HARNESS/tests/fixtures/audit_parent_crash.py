"""Isolated child subreaper proves parent-death termination without orphan tests.

Only starts known local audit workers. Never launches a robot owner or ROS.
Subreaper status is confined to this helper, not the pytest process.
"""
import ctypes,json,os,signal,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
role,phase,folder=sys.argv[1:4];area=Path(folder);area.mkdir(parents=True,exist_ok=True)

def parent():
    from audit_workers import AuditPool
    mode='init_hang' if phase=='initialize' else 'ignore_term' if phase=='busy' else 'normal'
    spec=dict(test_mode=mode,lifecycle_dir=str(area))
    command=[sys.executable,str(Path(__file__).with_name('audit_worker_fake.py'))]
    # Native smoke can supply a frozen numerical spec; it does not use devices.
    native=os.environ.get('F1_AUDIT_TEST_NUMERICS_SPEC')
    if native:
        spec=json.loads(Path(native).read_text());command=None
    p=AuditPool(spec,area/'workers',_worker_command=command)
    p.start(timeout_s=15)
    if native:
        for row in p.children:
            (area/(str(row['index'])+'.json')).write_text(json.dumps(dict(pid=row['process'].pid,phase='idle')))
    (area/'ready').write_text(p.generation)
    if phase=='busy':
        import numpy as np
        from f1_harness.skills import SkillGeometry
        scene=SkillGeometry(base_eef=np.eye(4),base_object=np.eye(4),base_hole=np.eye(4),eef_object=None,
            object_grasp_eef=np.eye(4),approach_clearance_m=.1,transfer_clearance_m=.1,insertion_depth_m=.01,
            attached_radius_m=.02,uncertainty_m=.001,effective_clearance_m=.01,obstacles=(),grasp_opening=100.,release_opening=0.)
        job=([0.]*22,scene,[0.]*16,[([0.]*16,100,1.)])
        p.map([job,job],context_binding='a'*64,timeout_s=15)
    time.sleep(15)


def reaper():
    libc=ctypes.CDLL(None,use_errno=True)
    assert libc.prctl(36,1,0,0,0)==0  # PR_SET_CHILD_SUBREAPER, this helper only
    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'parent',phase,str(area)],
        stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    workers=[];reaped=[]
    def records():
        try:return [json.loads(p.read_text()) for p in sorted(area.glob('[01].json'))]
        except (ValueError,OSError):return []
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            rows=records()
            if len(rows)==2 and (phase=='initialize' or (area/'ready').exists()):
                if phase!='busy' or all(r['phase']=='busy' for r in rows):break
            if process.poll() is not None:raise AssertionError(process.stderr.read())
            time.sleep(.01)
        else:raise AssertionError('parent did not reach requested phase')
        workers=[r['pid'] for r in rows]
        if phase=='stopped':
            for pid in workers:os.kill(pid,signal.SIGSTOP)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if all(Path(f'/proc/{p}/stat').read_text().rsplit(')',1)[1].split()[0]=='T' for p in workers):break
                time.sleep(.01)
            else:raise AssertionError('workers did not stop before parent exit')
        process.kill();process.wait(timeout=3)
        deadline=time.monotonic()+3
        while len(reaped)<len(workers) and time.monotonic()<deadline:
            for pid in workers:
                if any(r['pid']==pid for r in reaped):continue
                got,status=os.waitpid(pid,os.WNOHANG)
                if got:
                    assert os.WIFSIGNALED(status)
                    reaped.append(dict(pid=pid,signal=os.WTERMSIG(status)))
            time.sleep(.01)
        assert len(reaped)==2 and all(r['signal']==signal.SIGKILL for r in reaped),reaped
        print(json.dumps(dict(phase=phase,parent_returncode=process.returncode,reaped=reaped,
            robot_connected=False,paid_model_calls=0)),flush=True)
    finally:
        if process.poll() is None:process.kill()
        process.wait(timeout=3)
        for row in records():
            pid=row['pid']
            if any(r['pid']==pid for r in reaped):continue
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:os.waitpid(pid,0)
            except ChildProcessError:pass
        process.stderr.close()

if __name__=='__main__':
    if role=='parent':parent()
    elif role=='reaper':reaper()
    else:raise SystemExit('unknown role')
