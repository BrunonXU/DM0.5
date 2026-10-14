"""Verified local Pico session + unique graph endpoint provenance for Jazzy.

This is deliberately not per-message DDS GID attribution. The deployed Jazzy
Python callback exposes only source/received timestamps, not publisher_gid.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
from pathlib import Path
import threading

EXPECTED_NAMES = {'arms':'pico_upper_bridge','left':'pico_upper_teleop','right':'pico_upper_teleop'}


class PicoSessionReader:
    def __init__(self,session_path,lock_path,expected_root,proc_root='/proc'):
        self.session_path=Path(session_path)
        self.lock_path=Path(lock_path)
        self.expected_root=Path(expected_root).resolve()
        self.proc=Path(proc_root)

    def _process(self,pid,module,parent=None):
        if type(pid) is not int or pid<=0:raise ValueError('invalid Pico PID')
        path=self.proc/str(pid)
        parts=(path/'stat').read_text().rsplit(')',1)[1].split()
        ppid,start=int(parts[1]),parts[19]
        args=(path/'cmdline').read_bytes().split(b'\0')
        module_pair = any(a==b'-m' and b==module.encode() for a,b in zip(args,args[1:]))
        if not module_pair or (parent is not None and ppid!=parent):
            raise ValueError('Pico process identity/parent mismatch')
        if (path/'cwd').resolve()!=self.expected_root:
            raise ValueError('Pico process working directory differs from deployed source')
        return {'pid':pid,'start':start,'module':module,'ppid':ppid}

    def read(self):
        try:
            raw=self.session_path.read_bytes()
            state=json.loads(raw)
            if not isinstance(state,dict):raise ValueError('Pico session must be a JSON object')
            if state.get('owner')!='pico_upper' or state.get('mode')!='arms_and_grippers':
                raise ValueError('independent Pico session is not active')
            if Path(state['source_root']).resolve()!=self.expected_root:
                raise ValueError('Pico source release does not match deployed current')
            supervisor=self._process(state['pid'],'pico_upper.supervisor')
            if supervisor['start']!=state.get('process_start'):
                raise ValueError('Pico supervisor PID was reused')
            args=(self.proc/str(state['pid'])/'cmdline').read_bytes().split(b'\0')
            if b'start' not in args:raise ValueError('Pico supervisor is not a start session')
            with self.lock_path.open('r') as probe:
                try:
                    fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:
                    fcntl.flock(probe,fcntl.LOCK_UN)
                    raise ValueError('Pico control lock is not held')
                owner=json.load(probe)
            if not isinstance(owner,dict):raise ValueError('Pico control lock owner must be a JSON object')
            if owner.get('owner')!='pico_upper' or owner.get('pid')!=state['pid'] or owner.get('process_start')!=state['process_start']:
                raise ValueError('Pico session and control lock owner differ')
            children_file=self.proc/str(state['pid'])/'task'/str(state['pid'])/'children'
            children=[int(value) for value in children_file.read_text().split()]
            verified=[]
            for module in ('pico_upper.bridge','pico_upper.teleop'):
                matches=[]
                for child in children:
                    try:matches.append(self._process(child,module,state['pid']))
                    except (OSError,ValueError,IndexError):pass
                if len(matches)!=1:raise ValueError('need exactly one live '+module+' child')
                verified.extend(matches)
            lock_stat=self.lock_path.stat()
            for process in [supervisor,*verified]:
                owns_inode=False
                for fd in (self.proc/str(process['pid'])/'fd').iterdir():
                    try:
                        found=fd.stat()
                        if (found.st_dev,found.st_ino)==(lock_stat.st_dev,lock_stat.st_ino):owns_inode=True;break
                    except OSError:pass
                if not owns_inode:raise ValueError('verified Pico process does not hold the shared lock inode')
                if self._process(process['pid'],process['module'],process.get('ppid') if process is not supervisor else None)!=process:
                    raise ValueError('Pico child changed during proof')
            if self.session_path.read_bytes()!=raw or self._process(state['pid'],'pico_upper.supervisor')!=supervisor:
                raise ValueError('Pico session changed while checking')
            return {'supervisor':supervisor,'children':verified,'source_root':str(self.expected_root),
                    'session_started_at':state.get('started_at')}
        except (OSError,KeyError,TypeError,IndexError,json.JSONDecodeError) as exc:
            raise ValueError('independent Pico session could not be verified: '+str(exc)) from exc


class PicoSourceEpoch:
    def __init__(self,reader,stable_s=1.0):
        self.reader=reader
        self.stable_s=stable_s
        self.lock=threading.RLock()
        self.epoch=0
        self.valid=False
        self.reason='waiting for verified independent Pico session'
        self.signature=None
        self.since=None
        self.checked_mono=None
        self.last_ros_ns=None
        self.start_ros_ns=None
        self.gids={}
        self.proof=None

    def update(self,graph,ros_ns,mono):
        with self.lock:
            try:
                identity=self.reader.read()
                gids={}
                for kind,name in EXPECTED_NAMES.items():
                    entries=graph.get(kind,[])
                    if len(entries)!=1 or entries[0].get('name')!=name:
                        raise ValueError('need unique independent Pico writer: '+kind)
                    gid=bytes(entries[0].get('gid',[]))
                    if not gid or not any(gid):raise ValueError('empty graph endpoint identity: '+kind)
                    gids[kind]=gid
                signature=hashlib.sha256(json.dumps([identity,{key:value.hex() for key,value in gids.items()}],sort_keys=True).encode()).hexdigest()
                if self.last_ros_ns is not None and ros_ns<self.last_ros_ns:
                    self.signature=None;self.valid=False;self.epoch+=1
                    raise ValueError('ROS clock moved backwards')
                if signature!=self.signature:
                    self.signature=signature;self.since=mono;self.valid=False;self.start_ros_ns=None;self.epoch+=1
                self.gids=gids
                self.proof={"identity":identity,"observed_graph_gids":{key:value.hex() for key,value in gids.items()},"checked_ros_ns":ros_ns,"source_basis":"verified_pico_session_and_graph"}
                if not self.valid and mono-self.since>=self.stable_s:
                    self.valid=True;self.start_ros_ns=ros_ns;self.epoch+=1
                self.reason=None if self.valid else 'waiting for one-second stable Pico session and writers'
            except ValueError as exc:
                if self.valid or self.signature is not None:self.epoch+=1
                self.valid=False;self.signature=None;self.since=None;self.start_ros_ns=None;self.gids={};self.reason=str(exc)
            self.last_ros_ns=ros_ns;self.checked_mono=mono
            return self.valid

    def admit(self,kind,info,ros_ns,mono):
        with self.lock:
            if not self.valid or self.checked_mono is None or not 0<=mono-self.checked_mono<=.5:
                raise ValueError(self.reason or 'Pico session/writer verification is stale')
            if self.last_ros_ns is not None and ros_ns<self.last_ros_ns:
                self.valid=False;self.epoch+=1;self.reason='ROS clock moved backwards during command receipt'
                raise ValueError(self.reason)
            if kind not in self.gids:raise ValueError('unknown command source')
            if not isinstance(info,dict):raise ValueError('DDS receipt metadata missing')
            stamp=info.get('received_timestamp')
            if type(stamp) is not int or stamp<=0 or stamp<self.start_ros_ns or stamp>ros_ns or ros_ns-stamp>50_000_000:
                raise ValueError('DDS receipt timestamp outside current verified Pico epoch')
            return {'stamp_ns':stamp,'writer_gid':self.gids[kind], 'observed_graph_gid':self.gids[kind].hex(),'epoch':self.epoch,
                    'source_basis':'verified_pico_session_and_graph','session_identity':self.signature}
