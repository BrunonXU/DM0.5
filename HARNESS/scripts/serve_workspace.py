#!/usr/bin/env python3
"""Serve the existing infra task page. Starting this service never starts a robot owner."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from f1_harness.collect import ssh_reader_command
from f1_harness.site import _safe_environment
from f1_harness.web_execution import WorkspaceSupervisor, workspace_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--no-tunnel', action='store_true', help='Local integration tests only')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    # An endpoint belongs to this supervisor lifetime, never persisted config.
    config.pop('_numerical_pool_socket', None)
    config.pop('_model_pool_socket', None)
    config.pop('_subscription_model_pool_sockets', None)
    args.runtime.mkdir(parents=True, exist_ok=True)
    lock = (args.runtime / 'workspace.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    supervisor = WorkspaceSupervisor(config)
    server = workspace_server(supervisor)
    tunnel = None
    worker = None
    boot_client = None
    numerical_client = None
    model_pool = None
    model_directory = None
    subscription_pools = []
    sam_client = None
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        if config.get('warm_model_connection') is True:
            import tempfile, tomllib
            from f1_harness.model_pool import ModelPool
            model_directory = Path(tempfile.mkdtemp(prefix='f1-model-pool-'))
            settings=tomllib.loads((Path(config['root'])/'configs/planner.example.toml').read_text())
            credential=Path(settings['planner']['credentials_file'])
            if not credential.is_absolute(): credential=Path(config['root'])/credential
            try:
                model_pool=ModelPool(model_directory/'network.sock',credential).start()
                supervisor.config['_model_pool_socket']=str(model_pool.path)
            except (OSError,ValueError):
                if model_pool: model_pool.close()
                model_pool=None
        for mode, credential in config.get('chatgpt_subscription_credentials', {}).items():
            if mode not in ('astra', 'vla_astra') or not Path(credential).is_absolute():
                raise ValueError('invalid_subscription_configuration')
            import tempfile
            from f1_harness.model_pool import ModelPool
            directory = Path(tempfile.mkdtemp(prefix='f1-subscription-pool-'))
            pool = ModelPool(directory/'network.sock', None, subscription_path=credential).start()
            subscription_pools.append((pool, directory))
            supervisor.config.setdefault('_subscription_model_pool_sockets', {})[mode] = str(pool.path)
        if config.get('sam_service'):
            from f1_harness.sam_lifecycle import WarmSam
            sam_client=WarmSam(config,args.runtime)
            sam_client.start()
        if not args.no_tunnel:
            ssh = ssh_reader_command({}, host=config['host'], port=config['port'],
                identity=config['identity'], known_hosts=config['known_hosts'])[:-1]
            command = ssh[:-1] + ['-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=5',
                '-o', 'ServerAliveCountMax=3', '-N', '-R', '127.0.0.1:19843:127.0.0.1:19843', ssh[-1]]
            tunnel = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=(args.runtime / 'tunnel.log').open('ab'),
                env=_safe_environment(), start_new_session=True)
            if stopped.wait(1) or tunnel.poll() is not None:
                raise RuntimeError('infra_reverse_tunnel_unavailable')
        if config.get('warm_boot_recheck') is True:
            from f1_harness.boot_client import WarmBootClient
            boot_client=WarmBootClient(config,args.runtime)
            try:
                boot_client.start();supervisor.boot_client=boot_client
            except (OSError,ValueError):
                # Warm discovery is an optimization; no request was dispatched.
                boot_client.close();boot_client=None
        if config.get('warm_numerics') is True and not args.no_tunnel:
            from f1_harness.numerical_pool_client import WarmNumericalClient
            numerical_client=WarmNumericalClient(config,args.runtime)
            try:
                # The optional socket never replaces the task's private socket.
                # Not ready/failed/incompatible prewarm falls back at service init.
                supervisor.config['_numerical_pool_socket']=numerical_client.start()
            except (OSError,ValueError):
                numerical_client.close();numerical_client=None
        if any(supervisor.runs_root.glob('web-*/export-job-v2.json')):
            from f1_harness.export_worker import launch
            launch(supervisor.runs_root)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        (args.runtime / 'service.json').write_text(json.dumps(dict(pid=os.getpid(), started_at=time.time(),
            config=str(args.config.resolve()), port=19843, owner_started=False, motion_authorized=False)) + '\n')
        print(json.dumps(dict(ready=True, service='infra_workspace', owner_started=False, motion_authorized=False)), flush=True)
        while not stopped.wait(.5):
            if tunnel is not None and tunnel.poll() is not None:
                raise RuntimeError('infra_reverse_tunnel_lost')
    finally:
        # An in-flight task gets its explicit cancel path; no next task is auto-resumed.
        supervisor.begin_shutdown()
        if worker is not None:
            server.shutdown()
        server.server_close()
        # Process waits are already bounded in the lifecycle. Do not tear down
        # the only task supervisor halfway through its owner cancellation path.
        if supervisor.thread and supervisor.thread.is_alive():
            supervisor.thread.join()
        if model_pool is not None:model_pool.close()
        for pool, directory in subscription_pools:
            pool.close()
            try: directory.rmdir()
            except OSError: pass
        if model_directory is not None:
            try: model_directory.rmdir()
            except OSError: pass
        if boot_client is not None:boot_client.close()
        if numerical_client is not None:numerical_client.close()
        if sam_client is not None:sam_client.close()
        if tunnel is not None and tunnel.poll() is None:
            os.killpg(tunnel.pid, signal.SIGTERM)
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(tunnel.pid, signal.SIGKILL)
        lock.close()


if __name__ == '__main__':
    main()
