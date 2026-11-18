"""Common launch commands, readiness checks, and cleanup for Harness auxiliary processes."""

import json
import os
import shlex
import signal
import socket
import subprocess
from pathlib import Path, PurePosixPath

from .collect import ssh_reader_command

# SSH stdin is deliberately kept open. Disconnect/EOF requests clean SIGINT on
# this one native services group; its existing supervisor reaps its own children.
REMOTE_GUARD = r"""
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
"""


def commands(args, root, run):
    site = Path(args.local_site).resolve()
    run = Path(run).resolve()
    root = Path(root).resolve()
    for path, parent in (
        (args.remote_site, "/home/user/Bruno/HARNESS/site"),
        (args.remote_bundle, "/home/user/Bruno/HARNESS/development"),
    ):
        value = PurePosixPath(path)
        if not value.is_relative_to(parent) or ".." in value.parts or str(value) == parent:
            raise ValueError("explicit_harness_remote_path_required")
    gateway = json.loads((site / "gateway.local.json").read_text())
    masks = json.loads((site / "masks.local.json").read_text())
    mask_execution = gateway.get("scene", {}).get("mask_execution", "remote")
    if mask_execution not in ("remote", "local_fixed_color", "local_sam2"):
        raise ValueError("unknown_mask_execution")
    if (gateway.get("control_port"), gateway.get("image_port"), masks.get("port")) != (19840, 19841, 19842):
        raise ValueError("launcher_requires_registered_19840_19841_19842_ports")
    ssh = ssh_reader_command(
        {}, host=args.host, port=args.port, identity=args.identity, known_hosts=args.known_hosts
    )[:-1]
    native = "/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python"
    remote_run = "/home/user/Bruno/HARNESS/runs/services-" + run.name
    entry = [native, args.remote_bundle + "/native/entry.py"]
    service_argv = entry + ["--run", remote_run, "services", args.remote_site, "--mode", "hardware"]
    pool = getattr(args, "numerical_pool", None)
    if pool:
        service_argv += ["--numerical-pool", str(pool)]
    remote = "source /opt/ros/jazzy/setup.bash\nexec " + shlex.join(service_argv)
    rows = [
        {
            "name": "masks",
            "argv": [
                str(root / "perception/.venv/bin/python"),
                str(root / "native/entry.py"),
                "--run",
                str(run / "masks"),
                "serve",
                "masks",
                str(site / "masks.local.json"),
            ],
        },
        {
            "name": "control_tunnel",
            "port": 19840,
            "argv": ssh[:-1]
            + ["-o", "ExitOnForwardFailure=yes", "-N", "-L", "127.0.0.1:19840:127.0.0.1:19840", ssh[-1]],
        },
        {
            "name": "image_tunnel",
            "port": 19841,
            "argv": ssh[:-1]
            + [
                "-o",
                "ExitOnForwardFailure=yes",
                "-N",
                "-L",
                "127.0.0.1:19841:127.0.0.1:19841",
                "-R",
                "127.0.0.1:19842:127.0.0.1:19842",
                ssh[-1],
            ],
        },
        {
            "name": "robot_services",
            "remote": True,
            "argv": ssh + [shlex.join(["python3", "-c", REMOTE_GUARD, "bash", "-c", remote])],
        },
    ]
    if mask_execution in ("local_fixed_color", "local_sam2"):
        rows = rows[1:]
    if mask_execution == "local_sam2":
        import urllib.request

        expected = next(iter(masks["views"].values()))["checkpoint_sha256"]
        health = json.load(urllib.request.urlopen("http://127.0.0.1:19842/health", timeout=2))
        if health.get("ready") is not True or health.get("model_sha256") != expected:
            raise ValueError("sam_service_not_ready")
    if mask_execution == "local_fixed_color":
        image = next(row for row in rows if row["name"] == "image_tunnel")
        reverse = image["argv"].index("-R")
        del image["argv"][reverse : reverse + 2]
    owner = "source /opt/ros/jazzy/setup.bash\n" + shlex.join(
        entry + ["--run", remote_run + "-owner", "owner", args.remote_site, "--mode", "hardware"]
    )
    planner = shlex.join(
        [
            str(root / ".venv/bin/python"),
            "-m",
            "f1_harness.cli",
            "--run",
            str(run / "planner"),
            "run-task",
            str(site / "task.local.json"),
            "--gateway",
            "http://127.0.0.1:19840",
            "--image-gateway",
            "http://127.0.0.1:19841",
            "--planner",
            "astra",
        ]
    )
    return rows, {
        "owner_command": owner,
        "planner_command": planner,
        "remote_services_run": remote_run,
        "windows_tunnel": "ssh -F NUL -N -o ExitOnForwardFailure=yes -L 127.0.0.1:7907:127.0.0.1:7906 user@192.0.2.7",
        "windows_sender": 'cd "$HOME/DAgger-F1"\n.\\scripts\\windows_f1_sender.cmd',
    }


def required_ports(rows):
    return (19840, 19841, 19842) if any(row["name"] == "masks" for row in rows) else (19840, 19841)


def require_free_ports(ports=(19840, 19841, 19842)):
    sockets = []
    try:
        for port in ports:
            item = socket.socket()
            sockets.append(item)
            # Match SSH's listener semantics. A closed previous tunnel may leave
            # accepted connections in TIME_WAIT; that is not a live port owner.
            # listen() still rejects an actually occupied endpoint. The real
            # SSH launch also retains ExitOnForwardFailure for races afterward.
            item.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                item.bind(("127.0.0.1", port))
                item.listen(1)
            except OSError as exc:
                raise ValueError("local_port_already_in_use:" + str(port)) from exc
    finally:
        for item in sockets:
            item.close()


def ready(row, log):
    if "port" in row:
        try:
            with socket.create_connection(("127.0.0.1", row["port"]), timeout=0.1):
                return True
        except OSError:
            return False
    for line in log.read_text(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and value.get("ready") is True
            and (value.get("service") == row["name"] or row.get("remote") and "services" in value)
        ):
            return True
    return False


def stop_owned(children):
    for row, process, log in reversed(children):
        if process.stdin:
            process.stdin.close()  # EOF lets the remote native group clean up first.
        if process.poll() is None:
            if not row.get("remote"):
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=15 if row.get("remote") else 7)
            except subprocess.TimeoutExpired:
                row["cleanup_timeout"] = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        row["returncode"] = process.returncode
        log.close()
