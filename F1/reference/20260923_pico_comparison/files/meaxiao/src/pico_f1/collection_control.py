"""Short-lived central session controller invoked only via restricted SSH keys."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from pico_f1.transfer import DEFAULT_BASE, ROBOTS, atomic_json, read, lock, destination_path

TTL = 90
RECEIVER = "pico-transfer-receiver.service"
DASHBOARD = "pico-dashboard-central.service"


def allowed_destination(value):
    # The robot may choose dataset/scene names within the approved pico storage,
    # not arbitrary server directories. The mounted storage root must exist.
    storage = DEFAULT_BASE.parent.resolve(strict=True)
    dest = Path(os.path.abspath(value))
    base = dest.parent.parent.resolve()
    if not base.is_relative_to(storage) or base == storage:
        raise ValueError("中央dataset_base必须在已授权pico存储目录之内")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination_path(dest, base)


def systemctl(action, units):
    subprocess.run(["systemctl", "--user", action, *units], check=True, timeout=18,
                   stdout=subprocess.DEVNULL)


def reconcile_dashboard(home, leases, service=systemctl):
    """Manual viewer owns its selection until released; never starts receiver."""
    manual = home / 'dashboard_manual.json'
    viewers = sorted((r, v) for r, v in leases.items() if v['dashboard'])
    selected = read(manual)['root'] if manual.exists() else (viewers[0][1]['destination'] if viewers else None)
    if selected is None:
        service('stop', [DASHBOARD])
        return
    view_path = home / 'dashboard_selection.json'
    old = read(view_path) if view_path.exists() else {}
    atomic_json(view_path, dict(root=selected, manual=manual.exists(),
                               active_scenes=sorted({v['destination'] for v in leases.values()})))
    service('restart' if old.get('root') != selected else 'start', [DASHBOARD])


def manual_view(project, operation, root=None, service=systemctl, now=None):
    home = Path(project).parent / 'transfer_6000d'
    marker = home / 'dashboard_manual.json'
    now = time.time() if now is None else now
    with lock(home / 'collection_control.lock'):
        leases_path = home / 'collection_leases.json'
        leases = read(leases_path) if leases_path.exists() else {}
        leases = {k: v for k, v in leases.items() if v['expires'] > now}
        if operation == 'start':
            selection = home / 'dashboard_selection.json'
            candidate = root or (read(selection)['root'] if selection.exists() else str(DEFAULT_BASE/'mm/mocap_data'))
            if not Path(candidate).is_dir():
                raise ValueError('数据集目录不存在，独立查看不会创建或采集新数据')
            selected = allowed_destination(candidate)
            atomic_json(marker, dict(root=str(selected), started=now))
        elif operation == 'stop':
            if marker.exists():
                marker.unlink()
        else:
            raise ValueError('unknown manual operation')
        # On a service failure retain the requested state so the same command
        # can retry; never report success or touch robot collection leases.
        reconcile_dashboard(home, leases, service)
        return dict(manual=marker.exists(), active_robots=sorted(leases),
                    dashboard_needed=marker.exists() or any(v['dashboard'] for v in leases.values()))


def handle(project, identity, request, service=systemctl, now=None):
    now = time.time() if now is None else now
    if identity not in ROBOTS or request.get("robot") != identity:
        raise ValueError("key robot identity mismatch")
    token = str(uuid.UUID(request["token"]))
    operation = request["operation"]
    if operation not in ("start", "renew", "stop"):
        raise ValueError("operation not permitted")
    home = Path(project).parent / "transfer_6000d"
    marker = home / "collection_leases.json"
    with lock(home / "collection_control.lock"):
        if operation != 'stop' and (home/'scene_reset_in_progress.json').exists():
            raise ValueError('6000d场景重置未完成，保持停机并检查reset-report；不允许启动/续租')
        leases = read(marker) if marker.exists() else {}
        leases = {k: v for k, v in leases.items() if v["expires"] > now}
        existing = leases.get(identity)
        if operation == "stop":
            if existing and existing["token"] != token:
                raise ValueError("stale stop token cannot stop newer session")
            leases.pop(identity, None)
        else:
            destination = allowed_destination(request["destination"])
            if type(request.get("dashboard")) is not bool:
                raise ValueError("invalid dashboard flag")
            if existing and existing["token"] != token:
                raise ValueError("robot already has another live session")
            leases[identity] = dict(token=token, destination=str(destination),
                                    dashboard=request["dashboard"], expires=now + TTL)
        atomic_json(marker, leases)
        if leases:
            service("start", [RECEIVER])
            reconcile_dashboard(home, leases, service)
        elif (home / 'dashboard_manual.json').exists():
            service('stop', [RECEIVER])
            reconcile_dashboard(home, leases, service)
        else:
            service("stop", [RECEIVER, DASHBOARD])
        return dict(ok=True, active_robots=sorted(leases), ttl_s=TTL,
                    manual_dashboard=(home / 'dashboard_manual.json').exists())


def expire_leases(project):
    """Runs inside receiver. Stop idle workers after loss of all heartbeats."""
    home = Path(project).parent / "transfer_6000d"
    marker = home / "collection_leases.json"
    if not marker.exists():
        return
    with lock(home / "collection_control.lock"):
        leases = read(marker)
        alive = {k: v for k, v in leases.items() if v['expires'] > time.time()}
        if alive != leases:
            atomic_json(marker, alive)
        if not alive:
            units = [RECEIVER] if (home / 'dashboard_manual.json').exists() else [RECEIVER, DASHBOARD]
            subprocess.run(["systemctl", "--user", "--no-block", "stop", *units],
                           check=True, timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--robot", required=True, choices=ROBOTS)
    args = parser.parse_args()
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "pico-collection-v1":
        raise SystemExit("only pico-collection-v1 is allowed")
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise SystemExit("request too large")
    os.umask(0o077)
    try:
        result = handle(Path(__file__).resolve().parents[2], args.robot, json.loads(raw))
        print(json.dumps(result))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
