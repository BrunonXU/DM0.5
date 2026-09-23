"""Scene routing and scoped collection-session lifecycle; no motion operations."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import time
import uuid

from pico_f1.transfer import atomic_json, read, lock, ROBOTS


def load_settings(project):
    import yaml
    project = Path(project).resolve()
    cfg = yaml.safe_load((project / "config/collection.yaml").read_text())
    scene = cfg.get("scene", "")
    if not isinstance(scene, str) or not re.fullmatch("[A-Za-z0-9_-]+", scene):
        raise ValueError("scene必须是英文、数字、下划线或连字符")
    base = Path(cfg["local_base"])
    if base.is_absolute() or ".." in base.parts or not base.parts:
        raise ValueError("local_base必须为项目内相对路径")
    source = (project / base / scene / "mocap_data").resolve()
    if not source.is_relative_to(project):
        raise ValueError("local_base不能通过符号链接跳出项目")
    central = cfg["central"]
    if type(central.get('use_user_tailscale', False)) is not bool:
        raise ValueError('central.use_user_tailscale must be boolean')
    dest_base = Path(central["dataset_base"])
    if not dest_base.is_absolute() or ".." in dest_base.parts:
        raise ValueError("central.dataset_base必须是绝对路径")
    task = cfg.get("task", "")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task不可为空")
    for field in ("host", "user"):
        if not isinstance(central[field], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", central[field]) or central[field].startswith("-"):
            raise ValueError("invalid central SSH field")
    if type(central["port"]) is not int or not 1 <= central["port"] <= 65535:
        raise ValueError("invalid central SSH port")
    if type(central.get("dashboard")) is not bool:
        raise ValueError("central.dashboard必须为true或false")
    for field in ("identity_file", "known_hosts_file"):
        value = Path(central[field])
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("SSH凭据必须使用项目内相对路径")
    return dict(scene=scene, task=task.strip(), source=str(source),
                destination=str(dest_base / scene / "mocap_data"), central=central)


def effective_settings(project):
    marker = Path(project) / "run/collection_session.json"
    return read(marker)["settings"] if marker.exists() else load_settings(project)


def emit_env(project):
    cfg = effective_settings(project)
    for key, value in dict(PICO_F1_MOCAP_DIR=cfg["source"],
                           PICO_F1_RECORDING_TASK=cfg["task"], PICO_F1_SCENE=cfg["scene"]).items():
        print(f"export {key}={shlex.quote(value)}")


def control(project, session, operation):
    cfg = session["settings"]
    remote = cfg["central"]
    key = Path(project) / remote["identity_file"]
    hosts = Path(project) / remote["known_hosts_file"]
    if not key.is_file() or not hosts.is_file():
        raise ValueError("缺少受限SSH控制密钥/主机指纹，请先完成一次性配对")
    request = dict(operation=operation, robot=session["robot"], token=session["token"],
                   destination=cfg["destination"], dashboard=remote["dashboard"])
    cmd = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={hosts}", "-i", str(key), "-p", str(remote["port"])]
    if remote.get('use_user_tailscale', False):
        proxy = Path.home()/'.local/bin/tailscale-user'
        if not proxy.is_file():
            raise ValueError('缺少当前用户 tailscale-user；不回退到其他用户或系统 Tailnet')
        cmd += ['-o', f'ProxyCommand=env XDG_RUNTIME_DIR=/run/user/{os.getuid()} {shlex.quote(str(proxy))} nc %h %p']
    cmd += [f"{remote['user']}@{remote['host']}", "pico-collection-v1"]
    result = subprocess.run(cmd, input=json.dumps(request), text=True, capture_output=True, timeout=25)
    if result.returncode:
        raise RuntimeError("6000d启停请求失败：" + result.stderr.strip()[:500])
    response = json.loads(result.stdout)
    if not response.get("ok"):
        raise RuntimeError(str(response))
    return response


def stop_agent(project):
    project = Path(project).resolve()
    with lock(project / "transfer_state/launch.lock"):
        marker = project / "transfer_state/agent.json"
        if not marker.exists():
            return
        pid = int(read(marker)["pid"])
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            try:
                args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
            except FileNotFoundError:
                return
            expected = ["-u", "-m", "pico_f1.transfer", "agent", "--project", str(project)]
            if args[1:1+len(expected)] != expected:
                raise ValueError("传输PID身份不匹配，拒绝误停其他进程")
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except ProcessLookupError:
                return
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(5000):
                raise RuntimeError("传输队列尚未确认退出")
        finally:
            os.close(fd)


def bind_route(project, cfg):
    import pyarrow.parquet as pq
    source = Path(cfg["source"])
    legacy = Path(project) / "mocap_data/meta/info.json"
    if legacy.exists():
        raise ValueError("检测到旧平铺数据；请先迁移旧数据到对应场景，不能静默当成新数据集")
    source.mkdir(parents=True, exist_ok=True)
    route = source / ".collection_route.json"
    value = {k: cfg[k] for k in ("source", "destination", "scene", "task")}
    if route.exists() and read(route) != value:
        raise ValueError("已绑定的本地子数据集不能改任务或中央路径；请使用新的scene/local_base")
    if (source / "meta/tasks.parquet").exists():
        tasks = pq.read_table(source / "meta/tasks.parquet").to_pylist()
        if [r["task"] for r in tasks] != [cfg["task"]]:
            raise ValueError("任务描述与已有子数据集不一致，必须新建scene")
    atomic_json(route, value)
    # Registry lets the next session resume older scenes even if local_base changes.
    registry_path = Path(project) / "transfer_state/routes.json"
    routes = read(registry_path) if registry_path.exists() else {}
    routes[str(source)] = value
    atomic_json(registry_path, routes)


def start(project, robot):
    project = Path(project).resolve()
    marker = project / "run/collection_session.json"
    if marker.exists():
        raise ValueError("上次采集会话尚未关闭，请先 ./stop.sh；不要在运行时切换YAML")
    stop_agent(project)
    cfg = load_settings(project)
    bind_route(project, cfg)
    session = dict(robot=robot, token=str(uuid.uuid4()), settings=cfg, started=time.time())
    # Keep the token even if the SSH response is lost: stop/retry can release it.
    atomic_json(marker, session)
    response = control(project, session, "start")
    subprocess.run([str(project / ".venv/bin/python"), str(project / "scripts/ensure_transfer.py"),
                    "--robot", robot, "--source", cfg["source"], "--destination", cfg["destination"]], check=True)
    print(f"[数采会话] 场景={cfg['scene']} 本地={cfg['source']} 中央={cfg['destination']}")
    print(f"[自动传输] 6000d已启动；活跃机器人={response['active_robots']}")


def stop(project):
    project = Path(project).resolve()
    stop_agent(project)
    marker = project / "run/collection_session.json"
    if not marker.exists():
        print("[自动传输] 本地队列已关闭；没有本次中央会话记录")
        return
    session = read(marker)
    response = control(project, session, "stop")
    marker.unlink()
    print("[自动传输] 本地队列已关闭；未传数据保留，下次启动会继续补传")
    if response["active_robots"]:
        print(f"[自动传输] 其他机器人仍在采集，保留其中央服务：{response['active_robots']}")
    elif response.get('manual_dashboard'):
        print('[自动传输] 最后一台已退出，接收服务关闭；独立网页保持开启，可在6000d执行 ./dashboard_stop.sh 关闭')
    else:
        print("[自动传输] 最后一台已退出，6000d接收和网页服务均已关闭")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("env", "start", "stop", "show"))
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--robot", choices=ROBOTS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "env":
        emit_env(args.project)
    elif args.action == "show":
        print(json.dumps(effective_settings(args.project), ensure_ascii=False, indent=2))
    else:
        identity = Path("/home/user/.f1_robot_id").read_text().strip()
        if identity not in ROBOTS or (args.robot and identity != args.robot):
            raise ValueError("ROBOT_IDENTITY_MISMATCH")
        with lock(args.project / "transfer_state/session.lock"):
            if args.action == "start":
                start(args.project, identity)
            else:
                stop(args.project)


if __name__ == "__main__":
    main()
