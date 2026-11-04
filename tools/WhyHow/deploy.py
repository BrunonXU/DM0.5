#!/usr/bin/env python3
"""Publish only Why&How's files and its dedicated user service."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from urllib.request import urlopen

SOURCE = Path(__file__).resolve().parent
TARGET = Path("/home/user/Artifacts/Why&How")
UNIT = Path("/home/user/.config/systemd/user/why-how.service")
FILES = ["server.py", "catalog.json", "run.sh", "why-how.service", "README.md", "AGENTS.md"]


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main():
    assert Path.home() == Path("/home/user")
    paths = [SOURCE / name for name in FILES] + sorted((SOURCE / "web").rglob("*"))
    paths = [path for path in paths if path.is_file()]
    if any(path.is_symlink() for path in paths):
        raise RuntimeError("Refusing symlink source")
    if TARGET.is_symlink() or TARGET.resolve() != TARGET or UNIT.resolve() != UNIT:
        raise RuntimeError("Refusing symlink deployment path")
    existing = TARGET.exists()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if existing:
        if not (TARGET / "why-how.service").is_file():
            raise RuntimeError("Unknown content already exists at target")
        backup = TARGET.parent / ("Why&How-backup-" + timestamp)
        shutil.copytree(TARGET, backup)
    else:
        # Never replace another listener on the agreed port.
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", 4243))
    TARGET.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for source in paths:
        relative = source.relative_to(SOURCE)
        destination = TARGET / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".new")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
        manifest[str(relative)] = digest
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    if UNIT.exists() and UNIT.read_bytes() != (TARGET / "why-how.service").read_bytes():
        shutil.copy2(UNIT, UNIT.with_name("why-how.service.backup-" + timestamp))
    shutil.copy2(TARGET / "why-how.service", UNIT)
    command("systemctl", "--user", "daemon-reload")
    command("systemctl", "--user", "enable", "why-how.service")
    command("systemctl", "--user", "restart", "why-how.service")
    health = None
    for _ in range(40):
        try:
            with urlopen("http://127.0.0.1:4243/api/health", timeout=2) as response:
                health = json.load(response)
            break
        except OSError:
            time.sleep(0.25)
    assert health and health["ok"] and health["port"] == 4243
    report = {
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE), "target": str(TARGET), "files": manifest,
        "health": health,
        "service": command("systemctl", "--user", "show", "why-how.service",
                           "-p", "ActiveState", "-p", "MainPID", "-p", "UnitFileState"),
        "linger": command("loginctl", "show-user", "user", "-p", "Linger"),
    }
    (SOURCE / "artifacts").mkdir(exist_ok=True)
    (SOURCE / "artifacts/deployment.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
