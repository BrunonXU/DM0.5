"""Owned process lifecycle for a frozen infra run, with explicit private owner requests."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time

from .site import _safe_environment


def _planner_environment():
    """Planner networking only; workers retain their separate minimal environment."""
    environment = _safe_environment()
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
        if name in os.environ:
            environment[name] = os.environ[name]
    # The same planner also uses local robot tunnels. Never route those through
    # an external proxy when restoring the model connection's environment.
    bypass = [part.strip() for key in ('NO_PROXY', 'no_proxy')
              for part in environment.get(key, '').split(',') if part.strip()]
    bypass = list(dict.fromkeys(bypass + ['127.0.0.1', 'localhost', '::1']))
    environment['NO_PROXY'] = environment['no_proxy'] = ','.join(bypass)
    return environment


class WorkspaceProcesses:
    def __init__(self, config, run):
        override = config.get('planner_override')
        if override is not None and override != 'baseline':
            raise ValueError('invalid_planner_override')
        self.config, self.run = config, Path(run)
        self.root = Path(config['root'])
        spec = importlib.util.spec_from_file_location('_workspace_auxiliary', self.root / 'scripts/start_development.py')
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.children = []
        self.owner_channels = {}
        import threading
        self.owner_channel_lock = threading.Lock()
        self.args = argparse.Namespace(local_site=str(self.run / 'site'), remote_site=self.remote_site,
            remote_bundle=config['remote_bundle'], host=config['host'], port=config['port'],
            identity=config['identity'], known_hosts=config['known_hosts'],
            numerical_pool=config.get('_numerical_pool_socket'))
        self.rows, self.instructions = self.helper.commands(self.args, self.root, self.run)
        from .collect import ssh_reader_command
        self.ssh = ssh_reader_command({}, host=config['host'], port=config['port'],
            identity=config['identity'], known_hosts=config['known_hosts'])[:-1]

    @property
    def remote_site(self):
        return '/home/user/Bruno/HARNESS/site/' + self.run.name

    def copy_site(self):
        # A new directory, immutable per-run identity. No deletion or active-site overwrite.
        target = self.remote_site
        subprocess.run(self.ssh + ['mkdir ' + shlex.quote(target)], check=True, timeout=15,
                       capture_output=True)
        scp = ['scp', '-q', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UserKnownHostsFile=' + self.config['known_hosts'],
               '-i', self.config['identity'], '-P', str(self.config['port'])]
        subprocess.run(scp + [str(p) for p in sorted((self.run / 'site').glob('*.json'))]
                       + [self.config['host'] + ':' + target + '/'],
                       check=True, timeout=30, capture_output=True)

    def spawn(self, row):
        log = (self.run / (row['name'] + '.log')).open('xb')
        try:
            environment=_planner_environment() if row['name']=='planner' else _safe_environment()
            if row['name']=='planner':
                # Pin child imports to the same reviewed coordinator sources,
                # rather than the editable development installation's .pth.
                environment['PYTHONPATH']=str(self.root/'src')
                if self.config.get('planner_override') != 'baseline':
                    if self.config.get('_subscription_credentials'):
                        environment['F1_CHATGPT_SUBSCRIPTION_CREDENTIALS']=self.config['_subscription_credentials']
                    if self.config.get('_model_pool_socket'):
                        environment['F1_MODEL_POOL_SOCKET']=self.config['_model_pool_socket']
            process = subprocess.Popen(row['argv'], stdin=subprocess.PIPE, stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True)
        except BaseException:
            log.close()
            raise
        self.children.append((row, process, log))
        return process

    def check(self):
        for row, process, _ in self.children:
            if row.get('one_shot') and process.poll() == 0:
                continue
            exit_code=process.poll()
            if row['name'] != 'planner' and exit_code is not None:
                path = self.run / (row['name'] + '.log')
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 8192))
                    tail = stream.read().decode(errors='replace')
                # Native failures follow a large startup-contract report. Put
                # the structured exception first, before the UI's length cap.
                failure = None
                for line in reversed(tail.splitlines()):
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and value.get('error') and 'reason' in value:
                        failure = str(value['error']) + (': ' + str(value['reason']) if value['reason'] else '')
                        break
                detail=failure[:800] if failure else tail[-800:].strip()
                if not detail and row['name']=='owner' and exit_code==73:
                    detail='owner IPC路径已存在，启动被拒绝；已有端点未被自动删除。'
                elif not detail:
                    detail='组件未写出启动日志。'
                raise RuntimeError('运行组件退出：' + row['name'] + '（exit='+str(exit_code)+'）\n'+detail)

    def prepare_boot_recheck(self, stop):
        native = '/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python'
        argv = [native, self.config['remote_bundle'] + '/native/entry.py', '--run',
                self.instructions['remote_services_run'] + '-boot', 'boot-recheck', self.remote_site]
        setups = ('/opt/ros/jazzy/setup.bash',
                  '/home/user/workspace/f1/install/setup.bash',
                  '/home/user/workspace/agv/install/setup.bash',
                  '/home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.bash',
                  '/home/user/workspace/camera/ros2_ws/install/setup.bash')
        remote = ''.join('source ' + shlex.quote(path) + ' || exit\n' for path in setups)
        remote += 'exec ' + shlex.join(argv)
        row = dict(name='boot_recheck', one_shot=True, remote=True,
                   argv=self.ssh + [shlex.join(['python3', '-c', self.helper.REMOTE_GUARD,
                                              'bash', '-c', remote])])
        scp=['scp','-q','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
             '-o','UserKnownHostsFile='+self.config['known_hosts'],
             '-i',self.config['identity'],'-P',str(self.config['port'])]
        client=getattr(self,'boot_client',None)
        try:
            warm=bool(client is not None and client.capture(self.remote_site,self.expected_task_hash,stop))
        except Exception as exc:
            (self.run/'boot-route.json').write_text(json.dumps(dict(warm=True,
                outcome='failed_or_unknown',error_type=type(exc).__name__,motion_authorized=False))+'\n')
            if not isinstance(exc,InterruptedError):self._download_failed_boot_evidence(scp)
            raise
        (self.run/'boot-route.json').write_text(json.dumps(dict(warm=warm,motion_authorized=False))+'\n')
        process=None
        if not warm:
            process=self.spawn(row)
            deadline=time.monotonic()+25
            while process.poll() is None:
                if stop.wait(.1):raise InterruptedError('已取消启动复核。')
                if time.monotonic()>deadline:raise TimeoutError('只读启动复核超时。')
        if process is not None and process.returncode:
            self._download_failed_boot_evidence(scp)
        self.check()
        names = ('boot-recheck.json', 'boot-recheck-evidence.json', 'development.local.json')
        sources = [self.config['host'] + ':' + self.remote_site + '/' + name for name in names]
        subprocess.run(scp + sources + [str(self.run / 'site') + '/'],
                       check=True, timeout=10, capture_output=True)
        return json.loads((self.run/'site/boot-recheck.json').read_text())

    def _download_failed_boot_evidence(self,scp):
        # Failed warm and cold checks share the same evidence export path.
        # A copy failure must not replace the original validation failure.
        try:
            subprocess.run(scp+[self.config['host']+':'+self.remote_site+'/boot-recheck-evidence.json',
                str(self.run/'site/boot-recheck-evidence.json')],check=True,timeout=5,capture_output=True)
        except (OSError,subprocess.SubprocessError):pass

    def start_services(self, stop, *, owner_start=None):
        self.helper.require_free_ports(self.helper.required_ports(self.rows))
        waiting = []
        for row in self.rows:
            if stop.is_set():
                raise InterruptedError('已取消准备任务。')
            self.spawn(row)
            waiting.append((row, time.monotonic() + 60))
        # Owner initialization (model loading, read-only feedback and recording
        # setup) overlaps auxiliary constructors. START remains a separate step
        # after every component and the prepared task have been checked.
        if owner_start is not None:
            if stop.is_set():raise InterruptedError('已取消准备任务。')
            owner_start()
        # Tunnels, segmentation and native auxiliary constructors do not require
        # each other's readiness. All must be healthy before starting the owner.
        for row, deadline in waiting:
            while not self.helper.ready(row, self.run / (row['name'] + '.log')):
                self.check()
                if stop.wait(.1):
                    raise InterruptedError('已取消准备任务。')
                if time.monotonic() > deadline:
                    raise TimeoutError('辅助服务未就绪：' + row['name'])
        self.check()
        if stop.is_set():
            raise InterruptedError('已取消准备任务。')

    def start_services_with_owner(self, stop, owner_start):
        self.start_services(stop,owner_start=owner_start)

    def start_owner(self):
        native = '/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python'
        argv = [native, self.config['remote_bundle'] + '/native/entry.py', '--run',
                self.instructions['remote_services_run'] + '-owner', 'owner', self.remote_site,
                '--mode', 'hardware', '--web-control']
        socket_path = json.loads((self.run / 'site/owner.local.json').read_text())['owner_socket']
        controller = json.loads((self.run / 'site/controller.local.json').read_text())
        model_check = ('bash /home/user/Bruno/DAgger/start_f1_toys_inference.sh '
                       '--model stageb1250 --check || exit\n'
                       if controller.get('hybrid', {}).get('enabled') else '')
        remote = ('test ! -e ' + shlex.quote(socket_path) + ' || exit 73\n'
                  + model_check +
                  'source /opt/ros/jazzy/setup.bash || exit\n'
                  'source /home/user/workspace/f1/install/setup.bash || exit\n'
                  'source /home/user/workspace/agv/install/setup.bash || exit\n'
                  'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp\n'
                  'export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml\n'
                  'export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\n'
                  'unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS\n'
                  'exec ' + shlex.join(argv))
        row = dict(name='owner', remote=True, argv=self.ssh + [shlex.join(
            ['python3', '-c', self.helper.REMOTE_GUARD, 'bash', '-c', remote])])
        return self.spawn(row)

    def owner_call(self, method, **kwargs):
        from .owner_transport import OwnerChannel
        native = '/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python'
        command = shlex.join([native, self.config['remote_bundle'] + '/native/owner_request.py',
                             self.remote_site, '--stream'])
        # A blocked status query cannot occupy the stop/start channel. Both
        # remain private to this supervisor; planner RPC exposes neither.
        lane = 'status' if method == 'status' else 'control'
        with self.owner_channel_lock:
            if lane not in self.owner_channels:
                self.owner_channels[lane] = OwnerChannel(self.ssh + [command],
                    self.run / ('owner-' + lane + '-transport.log'))
            channel = self.owner_channels[lane]
        return channel.call(method, **kwargs)

    def start_planner(self, request):
        mode = request['config']['mode']
        if mode not in ('astra','vla_astra'):raise ValueError('unknown_task_strategy')
        override = self.config.get('planner_override')
        if override is not None:
            if override != 'baseline':raise ValueError('invalid_planner_override')
            if mode == 'vla_astra':raise ValueError('planner_override_incompatible_with_vla_astra')
        effective_planner = override or mode
        config = self.root / 'configs/planner.example.toml'
        hardware = self.run / 'planner.toml'
        hardware.write_text(config.read_text().replace('mode = "offline"', 'mode = "hardware"'))
        argv = [str(self.root / '.venv/bin/python'), '-m', 'f1_harness.cli', '--config', str(hardware),
                '--run', str(self.run / 'planner'), 'run-task', str(self.run / 'site/task.local.json'),
                '--workspace-config', str(self.run / 'site/workspace-config.json'),
                '--authorization-source', 'web', '--gateway', 'http://127.0.0.1:19840',
                '--image-gateway', 'http://127.0.0.1:19841', '--planner', effective_planner]
        from .memory import ExperienceStore
        from .evidence import EvidenceStore
        try:
            snapshot=ExperienceStore(Path(self.config.get('experience_root',self.root/'experience'))).snapshot()
        except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
            EvidenceStore(self.run).event('experience_snapshot_unavailable',error=type(exc).__name__,
                                          executable=False,zero_experience_fallback=True)
        else:
            EvidenceStore(self.run).event('experience_snapshot_frozen',**snapshot)
            argv.extend(['--experience-snapshot',snapshot['path']])
        return self.spawn(dict(name='planner', argv=argv))

    def stop_planner(self):
        for row, process, _ in self.children:
            if row['name'] == 'planner' and process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)

    def close(self):
        try:
            self.helper.stop_owned(self.children)
        finally:
            for channel in self.owner_channels.values():
                channel.close()

    def collect(self):
        # Reuse the existing owner identity / original recording / seal implementation.
        from .collect import collect_rollout
        return collect_rollout(self.run / 'planner', self.run / 'collected',
            host=self.config['host'], port=self.config['port'], identity=Path(self.config['identity']),
            known_hosts=Path(self.config['known_hosts']), seal=True, render_video=True)

    def seal_collection(self):
        from .collect import prepare_collection
        return prepare_collection(self.run / 'planner', **{key: self.config[key]
            for key in ('host', 'port', 'identity', 'known_hosts')})
