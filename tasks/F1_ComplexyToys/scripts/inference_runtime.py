#!/usr/bin/env python3
"""F1 deployment composition. Robot algorithms live in pinned DAgger/RLinf components.

build is offline; check/describe do not initialize hardware; run retains the
component's local Enter/START and shared lock. No checkpoint-specific branches.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(c):
    if c['schema'] != 'f1_inference_catalog_v1':
        raise ValueError('Unsupported catalog')
    if c['release'] != Path(c['robot_release']).name:
        raise ValueError('Assembly label differs from its immutable deployment path')
    names = set(c['models'])
    ports = set()
    identities = set()
    for key, m in c['models'].items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', key):
            raise ValueError('Unsafe model key')
        if m['alias'] in names:
            raise ValueError('Duplicate alias')
        names.add(m['alias'])
        if m['model_id'] in identities:
            raise ValueError('Duplicate model identity')
        identities.add(m['model_id'])
        for kind, port in [('robot', m['local_port']), (m['machine'], m['remote_port'])]:
            if not isinstance(port, int) or not 1024 <= port <= 65535 or (kind, port) in ports:
                raise ValueError('Invalid or duplicate port')
            ports.add((kind, port))
        c['machines'][m['machine']]
        c['contracts'][m['contract']]
        for mode, deployment in m['modes'].items():
            if mode not in ('rollout', 'hil', 'rlt-hil'):
                raise ValueError('Unsupported execution semantics')
            c['modes'][mode]
            alias = deployment.get('alias')
            if alias is not None:
                if not re.fullmatch(r'[a-zA-Z0-9_-]+', alias) or alias in names:
                    raise ValueError('Invalid or duplicate mode alias')
                names.add(alias)
            override = m['modes'][mode].get('client_component')
            if override is not None:
                if (not isinstance(override, dict) or set(override) != {'client', 'manifest_sha256'}
                        or not isinstance(override['client'], str) or not Path(override['client']).is_absolute()
                        or not isinstance(override['manifest_sha256'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', override['manifest_sha256'])):
                    raise ValueError('Invalid pinned client component override')
        if 'rlt-hil' in m['modes']:
            extension = m.get('rlt', {})
            for field in ('head', 'encoder'):
                if not isinstance(extension.get(field), str) or not Path(extension[field]).is_absolute():
                    raise ValueError('RLT requires explicit immutable asset paths')
            for field in ('model_id', 'model_version', 'identity', 'runtime_manifest', 'contract', 'label'):
                if not extension.get(field):
                    raise ValueError('RLT requires a verified composite identity')
            if extension.get('core_root') is not None and not Path(extension['core_root']).is_absolute():
                raise ValueError('RLT core must be an explicit pinned path')
            if type(extension.get('allow_boundary_updates', False)) is not bool:
                raise ValueError('Actor update switch must be boolean')
            if extension.get('snapshot_root') is not None and not Path(extension['snapshot_root']).is_absolute():
                raise ValueError('Actor snapshots require an absolute registry path')
            compute_path=extension.get('reference_compute_path','training_eager_prefix_and_suffix')
            if compute_path not in ('training_eager_prefix_and_suffix','serving_graph_prefix_eager_suffix','serving_graph_prefix_and_suffix'):
                raise ValueError('Unknown RLT reference compute path')
            if extension['runtime_manifest'].get('reference_compute_path',compute_path)!=compute_path:
                raise ValueError('RLT runtime manifest and compute configuration differ')
            if extension.get('allow_boundary_updates') and not extension.get('snapshot_root'):
                raise ValueError('Boundary updates require a snapshot registry')
            if extension['model_id'] in identities:
                raise ValueError('Duplicate RLT model identity')
            identities.add(extension['model_id'])
            identity = extension['identity']
            if identity.get('base_model_version') != m['model_version']:
                raise ValueError('RLT base model identity mismatch')
            rtc = extension.get('rtc', False)
            if type(rtc) is not bool:
                raise ValueError('Explicit RLT RTC boolean required')
            if identity.get('execution_mode') != ('rtc_c16_committed_prefix_v1' if rtc else 'sync_c16_hold_then_execute'):
                raise ValueError('Unsupported RLT execution contract')
            binding = extension['runtime_manifest'].get('binding', {})
            version = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if (extension['model_version'] != version or extension['model_id'] != ('f1-toys-rlt-rtc-' if rtc else 'f1-toys-rlt-sync-') + version[:16]
                    or any(binding.get(field) != identity.get(field) for field in (
                        'base_model_version', 'actor_manifest_sha256', 'encoder_manifest_sha256'))):
                raise ValueError('RLT composite identity does not match its runtime manifest')
            health = c['contracts'][extension['contract']]['health']
            if (health.get('rtc') is not rtc or health.get('sync') is not (not rtc)
                    or health.get('prediction_horizon') != 16 or health.get('execute_steps') != 16
                    or health.get('reference_horizon') != 50):
                raise ValueError('RLT requires an explicit C16/H50 execution contract')
            flags = c['modes']['rlt-hil']['rtc_flags']
            if '--rlt-hil' not in flags or '--episode-controls' not in flags or any(
                    flag in flags for flag in ('--rtc', '--rtc-hil')):
                raise ValueError('RLT mode must preserve separate base and actor execution contracts')
    if c['default_hil'] not in c['models'] or 'hil' not in c['models'][c['default_hil']]['modes']:
        raise ValueError('Default HIL must be explicitly registered')
    if 'default_rlt_hil' in c and ('rlt-hil' not in c['models'].get(c['default_rlt_hil'], {}).get('modes', {})):
        raise ValueError('Default RLT HIL must be explicitly registered')


def resolve(c, name):
    validate(c)
    matches = [key for key, m in c['models'].items() if key == name or m['alias'] == name
               or any(item.get('alias') == name for item in m['modes'].values())]
    if len(matches) != 1:
        raise ValueError(f'Unknown model {name}; use --list')
    return matches[0], c['models'][matches[0]]


def execution_mode(c, key, name, requested=None):
    """A mode alias always identifies its own execution contract."""
    aliases = [mode for mode, item in c['models'][key]['modes'].items()
               if name is not None and item.get('alias') == name]
    if aliases and requested is not None and requested != aliases[0]:
        raise ValueError('Mode alias conflicts with requested execution mode')
    mode = requested or (aliases[0] if aliases else 'rollout')
    if mode not in c['models'][key]['modes']:
        raise ValueError(f'{key}/{mode} has no accepted deployment')
    return mode


def client_component(c, key, mode):
    """Select only pinned client assets; RTC/control semantics remain mode-wide."""
    selected = dict(c['modes'][mode])
    if mode not in c['models'][key]['modes']:
        raise ValueError(f'{key}/{mode} has no accepted deployment')
    selected.update(c['models'][key]['modes'][mode].get('client_component', {}))
    return selected


def client_config(c, key, mode):
    m = c['models'][key]
    if mode not in m['modes']:
        raise ValueError(f'{key}/{mode} has no accepted deployment')
    cfg = copy.deepcopy(c['contracts'][m['contract']]['config'])
    if m['modes'][mode].get('evaluation'):
        cfg['evaluation'] = copy.deepcopy(m['modes'][mode]['evaluation'])
    if c.get('fault_lifecycle') == 'f1_fault_lifecycle_v1':
        cfg['fault_lifecycle'] = c['fault_lifecycle']
    cfg['policy'].update(endpoint=f"http://127.0.0.1:{m['local_port']}", model_id=m['model_id'],
                         rtc_pixels_directory=m['pixels'])
    if mode == 'rlt-hil':
        cfg['rtc_timing_replan'] = c.get('rtc_timing_replan', False)
        extension = m['rlt']
        cfg['rlt_policy'] = dict(cfg['policy'], endpoint=f"http://127.0.0.1:{m['local_port']}/rlt",
            model_id=extension['model_id'], model_version=extension['model_version'],
            chunk_steps=16, require_capabilities=True, expected_rlt=copy.deepcopy(extension['identity']))
        cfg['rlt_policy']['allow_boundary_updates'] = extension.get('allow_boundary_updates', False)
        if extension.get('rtc'):
            cfg['rlt_policy'].update(rlt_rtc=True, rlt_rtc_delay_steps=extension.get('rtc_delay_steps',6),
                                     rlt_rtc_min_steps=extension.get('rtc_min_steps',8))
    cfg['accepted_release_manifest_sha256'] = client_component(c,key,mode)['manifest_sha256']
    return cfg


def server_script(c, key):
    m = c['models'][key]; p = c['machines'][m['machine']]
    env = dict(p['env'], CUDA_VISIBLE_DEVICES=str(m['gpu']),
               TORCHINDUCTOR_CACHE_DIR=m['run']+'/cache/inductor', TRITON_CACHE_DIR=m['run']+'/cache/triton')
    cmd = ['python', '-u', p['server'], *p['flags'], '--port', str(m['remote_port']),
           '--bundle', m['bundle'], '--rlinf-root', p['rlinf_root'], '--dataset', p['dataset'],
           '--log', m['run']+'/logs/requests.jsonl']
    if 'rlt' in m:
        if m['rlt'].get('rtc'):
            cmd += ['--rlt-rtc']
        cmd += ['--rlt-head', m['rlt']['head'], '--rlt-encoder', m['rlt']['encoder']]
        if m['rlt'].get('core_root'):
            cmd += ['--rlt-rlinf-root', m['rlt']['core_root']]
        if m['rlt'].get('snapshot_root'):
            cmd += ['--rlt-snapshots', m['rlt']['snapshot_root']]
        if m['rlt'].get('reference_compute_path'):
            cmd += ['--rlt-reference-compute', m['rlt']['reference_compute_path']]
    return '\n'.join(['#!/usr/bin/env bash', 'set -euo pipefail', 'source '+shlex.quote(p['activate']),
                      *['export '+k+'='+shlex.quote(v) for k,v in env.items()],
                      'mkdir -p '+shlex.quote(m['run']+'/logs'), 'exec '+shlex.join(cmd), ''])


def build(c, output):
    validate(c)
    output.mkdir(parents=True, exist_ok=False)
    dump(output/'catalog.json', c)
    for name in ['inference_runtime.py', 'inference_terminal.py', 'inference_console.py', 'inference_console.sh',
                 'start_f1_inference.sh', 'start_f1_full_h20.sh', 'start_f1_rtc_hil.sh']:
        shutil.copyfile(Path(__file__).with_name(name), output/name)
    models = copy.deepcopy(c['archive'])
    for key, m in c['models'].items():
        (output/'servers').mkdir(exist_ok=True)
        server_entry=c['machines'][m['machine']]['release_root']+'/inference_runtime.py'
        (output/'servers'/f'{key}.sh').write_text('#!/usr/bin/env bash\nset -euo pipefail\nexec python3 '+shlex.join([server_entry,'--model',key,'--server'])+'\n')
        models['models'][m['model_id']] = dict(label=m['label'])
        if 'rlt' in m:
            models['models'][m['rlt']['model_id']] = dict(label=m['rlt']['label'])
            dump(output/'runtime-manifests'/f'{key}-rlt.json', m['rlt']['runtime_manifest'])
        for mode in m['modes']:
            dump(output/'configs'/f'{key}-{mode}.json', client_config(c,key,mode))
    dump(output/'rollout_models.json', models)
    manifest = {str(p.relative_to(output)):digest(p) for p in sorted(output.rglob('*')) if p.is_file()}
    dump(output/'assembly-manifest.json',dict(release=c['release'],files=manifest))


def verify_assembly(root):
    manifest = json.loads((root/'assembly-manifest.json').read_text())
    for rel, expected in manifest['files'].items():
        if digest(root/rel) != expected:
            raise ValueError(f'Assembly changed: {rel}')


def verify_component(c, mode, key=None):
    component = c['modes'][mode] if key is None else client_component(c,key,mode)
    root = Path(component['client'])
    manifest_path = root/'F1_RELEASE_MANIFEST.json'
    if digest(manifest_path) != component['manifest_sha256']:
        raise ValueError('Client manifest mismatch')
    manifest = json.loads(manifest_path.read_text())
    for rel, expected in manifest['files'].items():
        if digest(root/rel) != expected:
            raise ValueError(f'Pinned client changed: {rel}')


def check_health(c, key, mode=None):
    m = c['models'][key]
    with urlopen(f"http://127.0.0.1:{m['local_port']}/health",timeout=3) as response:
        health = json.load(response)
    expected = dict(c['contracts'][m['contract']]['health'], model_id=m['model_id'], model_version=m['model_version'])
    for k, v in expected.items():
        if health.get(k) != v:
            raise ValueError(f'{key}: server contract mismatch: {k}')
    if health.get('ik_recovery',{}).get('enabled'):
        raise ValueError('Experimental IK server cannot be used as standard')
    if mode == 'rlt-hil':
        extension = m['rlt']
        with urlopen(f"http://127.0.0.1:{m['local_port']}/rlt/health", timeout=3) as response:
            actor = json.load(response)
        expected_actor = dict(c['contracts'][extension['contract']]['health'],
            model_id=extension['model_id'], model_version=extension['model_version'],
            rlt=extension['identity'])
        for name, value in expected_actor.items():
            if actor.get(name) != value:
                raise ValueError(f'{key}: RLT server contract mismatch: {name}')
        if actor.get('ik_recovery', {}).get('enabled'):
            raise ValueError('Experimental IK server cannot be used as standard')
        return dict(base=health, actor=actor)
    return health


def tunnel_command(c, key):
    m=c['models'][key];t=c['machines'][m['machine']]['tunnel']
    return ['ssh','-NT','-p',str(t['port']),'-i',t['key'],
            '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','UserKnownHostsFile='+t['known_hosts'],'-o','ControlMaster=no','-o','ControlPath=none',
            '-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=5','-o','ServerAliveInterval=5',
            '-o','ServerAliveCountMax=3','-L',f"127.0.0.1:{m['local_port']}:127.0.0.1:{m['remote_port']}",t['host']]


def tunnel(c, key, root, supervise=False, mode=None):
    # Preserve old ownership lock paths: an existing supervisor remains sole owner.
    runtime = Path(c['models'][key]['tunnel_runtime']);runtime.mkdir(parents=True,exist_ok=True)
    if supervise:
        with (runtime/'supervisor.lock').open('a+') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return
            (runtime/'supervisor.pid').write_text(str(os.getpid()))
            while True:
                subprocess.run(tunnel_command(c,key),stdin=subprocess.DEVNULL,check=False)
                time.sleep(2)
    with (runtime/'tunnel.log').open('ab') as log:
        subprocess.Popen([sys.executable,str(root/'inference_runtime.py'),'--model',key,'--supervise'],
                         stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    for _ in range(15):
        try: return check_health(c,key,mode)
        except (OSError,ValueError) as error:
            failure=error;time.sleep(1)
    raise RuntimeError(f'Tunnel/health unavailable: {failure}; {runtime}/tunnel.log')


def client_command(c,key,mode,root,shadow=False,preflight=False,duration=None,continuous=False):
    m=c['models'][key];p=client_component(c,key,mode);client_config(c,key,mode)
    cmd=['bash',p['client']+'/scripts/50_f1_hil.sh',
         'preflight' if preflight else p['shadow'] if shadow else p['mode']]
    if not preflight:
        cmd += p['rtc_flags']+['--rtc-delay-steps',str(p['delay_steps']),'--rtc-min-steps',str(p['min_steps'])]
        if not shadow: cmd += ['--execute']
        if continuous or (p['default_continuous'] and duration is None):cmd += ['--continuous']
        if duration is not None:cmd += ['--duration-seconds',str(duration)]
    cmd += ['--purpose','policy_hil','--video','off','--recording-mode','hil-record',
            '--config',str(root/'configs'/f'{key}-{mode}.json'),'--runtime',m['modes'][mode]['runtime'],
            '--data-root',c['robot']['data_root'],'--task',c['contracts'][m['contract']]['prompt']]
    return cmd


def launch_script(c,key,mode,root,cmd):
    r=c['robot'];p=client_component(c,key,mode);m=c['models'][key];runtime=m['modes'][mode]['runtime']
    display = dict(model_id=m['model_id'],endpoint=f"http://127.0.0.1:{m['local_port']}",
                   label=m['label'],location=m['machine'].upper()+f" · GPU{m['gpu']}")
    if mode == 'rlt-hil':
        display.update(label=m['label']+' → '+m['rlt']['label'],
                       actor_model_id=m['rlt']['model_id'], execution_mode=m['rlt']['identity']['execution_mode'])
    metadata=json.dumps(display,ensure_ascii=False)
    lines=['set -eo pipefail','set -a','source '+shlex.quote(r['site_env']),'set +a']
    lines += ['source '+shlex.quote(path) for path in r['ros']]
    lines += ['unset F1_DAGGER_ENV F1_HIL_SESSION_CONFIG',
              'export F1_INFRA_ROOT='+shlex.quote(r['infra_root']),
              'export F1_DAGGER_RUNTIME='+shlex.quote(runtime),
              'export F1_INFERENCE_DISPLAY='+shlex.quote(metadata),
              'export PYTHONPATH='+shlex.quote(r['pythonpath'])+'${PYTHONPATH:+:$PYTHONPATH}',
              'mkdir -p '+shlex.quote(runtime),'set -u',
              'source '+shlex.quote(str(root/'inference_console.sh')),
              'f1_console_run '+shlex.quote(runtime+'/'+p['log'])+' '+shlex.join(cmd)]
    return '\n'.join(lines)


def supervised_terminal(c, key, mode, root):
    """SSH is a view into a persistent, uniquely locked control session.

    No command publishers are created here. The child still performs all
    startup checks and requires the genuine local START/Enter authorization.
    """
    if os.environ.get('F1_SUPERVISED_PROCESS') == '1':
        return
    if c.get('fault_lifecycle') != 'f1_fault_lifecycle_v1':
        return
    dependency = c.get('terminal_supervisor', {})
    tmux = dependency.get('executable') or shutil.which('tmux')
    if tmux is None:
        raise RuntimeError('tmux required for supervised control; no foreground fallback')
    if dependency and hashlib.sha256(Path(tmux).read_bytes()).hexdigest() != dependency['sha256']:
        raise RuntimeError('terminal supervisor identity changed')
    name = 'f1-policy-' + key + '-' + mode
    child = shlex.join(['env', 'F1_SUPERVISED_PROCESS=1', sys.executable,
                       str(root/'inference_runtime.py'), *sys.argv[1:]])
    from inference_terminal import attach
    status = Path(c['models'][key]['modes'][mode]['runtime'])/'status.json' if mode == 'rlt-hil' else None
    attach(tmux, name, child, status_path=status)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,default=Path(__file__).with_name('catalog.json'))
    parser.add_argument('--build',type=Path,help='Offline assembly only; never starts a service')
    parser.add_argument('--model')
    parser.add_argument('--mode',choices=['rollout','hil','rlt-hil'])
    parser.add_argument('--recovery',choices=['standard'],default='standard')
    action=parser.add_mutually_exclusive_group()
    for name in ['list','describe','check','shadow','preflight','supervise','server']:
        action.add_argument('--'+name,action='store_true')
    timing=parser.add_mutually_exclusive_group()
    timing.add_argument('--continuous',action='store_true')
    timing.add_argument('--duration-seconds',type=float)
    args=parser.parse_args();c=json.loads(args.catalog.read_text());validate(c)
    if args.build:build(c,args.build);return
    root=Path(__file__).resolve().parent
    if args.catalog.resolve() != root/'catalog.json':
        parser.error('Runtime catalog must belong to the verified assembly; use --build for new configuration')
    verify_assembly(root)
    if args.list:
        for key,m in c['models'].items():
            aliases = [m['alias'], *[item['alias'] for item in m['modes'].values() if item.get('alias')]]
            print(f"{key} ({','.join(aliases)}) · {m['label']} · {','.join(m['modes'])}")
        return
    default = c.get('default_rlt_hil') if args.mode == 'rlt-hil' else c['default_hil'] if args.mode == 'hil' else ''
    key,m=resolve(c,args.model or default)
    if args.server:
        for path, expected in c['machines'][m['machine']]['files_sha256'].items():
            if digest(path)!=expected:raise ValueError('Server component changed: '+path)
        os.execvp('bash',['bash','-c',server_script(c,key)])
    args.mode = execution_mode(c, key, args.model, args.mode)
    if args.duration_seconds is not None and (not math.isfinite(args.duration_seconds) or args.duration_seconds<=0):
        parser.error('duration must be positive and finite')
    if args.preflight and (args.continuous or args.duration_seconds is not None):parser.error('preflight has no execution duration')
    cmd=client_command(c,key,args.mode,root,args.shadow,args.preflight,args.duration_seconds,args.continuous)
    if args.describe:
        print(json.dumps(dict(model=key,client_command=cmd,server=server_script(c,key)),ensure_ascii=False,indent=2));return
    if args.supervise:tunnel(c,key,root,True);return
    if not (args.check or args.shadow or args.preflight) and not sys.stdin.isatty():
        parser.error('现场交互终端才能授权动作；远程检查用 --check 或 --shadow')
    if not (args.check or args.shadow or args.preflight):
        supervised_terminal(c, key, args.mode, root)
    verify_component(c,args.mode,key)
    preprocessing=c['contracts'][m['contract']]['health']['input_preprocessing']
    for name, field in [('contract.json','contract_sha256'),('processing.py','implementation_sha256')]:
        if digest(Path(m['pixels'])/name) != preprocessing[field]:
            raise ValueError('Robot image processor mismatch: '+name)
    health=tunnel(c,key,root,mode=args.mode)
    if args.check:
        identities = ({'model_id': health['base']['model_id'], 'actor_model_id': health['actor']['model_id']}
                      if args.mode == 'rlt-hil' else {'model_id': health['model_id']})
        print(json.dumps(dict(ready=True,model=key,mode=args.mode,release=c['release'],**identities),ensure_ascii=False));return
    runtime=Path(m['modes'][args.mode]['runtime']);runtime.mkdir(parents=True,exist_ok=True)
    dump(runtime/f'launch-{time.time_ns()}.json',dict(model=key,mode=args.mode,release=c['release'],
         assembly_sha256=digest(root/'assembly-manifest.json'),client_command=cmd,health=health,
         authorization=('zero_output' if args.shadow or args.preflight else
                        'awaiting_f710_start' if args.mode == 'rlt-hil' else 'awaiting_local_enter')))
    os.execvp('bash',['bash','-c',launch_script(c,key,args.mode,root,cmd)])


if __name__=='__main__':
    main()
