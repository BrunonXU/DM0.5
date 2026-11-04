#!/usr/bin/env python3
"""Finite 6000D -> H20 acceptance check. See README.md before rerunning."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new receipt filename")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    local = Path(tempfile.mkdtemp(prefix="h20-acceptance-"))
    remote = "/u01/user/staging/connect-acceptance-" + run_id
    socket_name = "h20check-" + run_id
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "Compression=no",
           "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
           "h20-train"]
    rsh = shlex.join(ssh[:-1])
    env = dict(os.environ, LC_ALL="C")
    report = {
        "started_at_utc": now.isoformat(),
        "started_at_shanghai": now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "run_id": run_id, "remote_directory": remote,
        "local_directory": str(local), "commands": [], "result": "FAIL",
    }
    remote_created = False
    interrupted_process = None

    def stop_process_group(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            return process.communicate(timeout=10)

    def run(argv, timeout=180, check=True):
        start = time.perf_counter()
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            stdout, stderr = stop_process_group(process)
            report["commands"].append({"argv": argv, "returncode": process.returncode,
                                       "stdout": stdout, "stderr": stderr,
                                       "error": "command interrupted or timed out"})
            raise
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        elapsed = time.perf_counter() - start
        report["commands"].append({"argv": argv, "elapsed_seconds": elapsed,
                                   "returncode": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise RuntimeError(f"Command failed ({result.returncode}): {shlex.join(argv)}: {result.stderr}")
        return result, elapsed

    def remote_py(code, check=True):
        return run(ssh + ["LC_ALL=C python3 -c " + shlex.quote(code)], check=check)[0]

    def sha(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def random_file(path, size_mib):
        digest = hashlib.sha256()
        with path.open("xb") as stream:
            for _ in range(size_mib):
                chunk = os.urandom(1024 * 1024)
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        return digest.hexdigest()

    def remote_sha(name):
        code = f"""import hashlib
h = hashlib.sha256()
with open({remote + '/' + name!r}, 'rb') as f:
    for b in iter(lambda: f.read(1048576), b''):
        h.update(b)
print(h.hexdigest())
"""
        return remote_py(code).stdout.strip()

    def heartbeat_state():
        return json.loads(remote_py(f"""import json
from pathlib import Path
p = Path({remote!r})
f = p / 'heartbeat.jsonl'
lines = f.read_text().splitlines() if f.exists() else []
print(json.dumps({{'ticks': len(lines), 'last': json.loads(lines[-1]) if lines else None,
                  'complete': (p / 'done.json').exists()}}))
""").stdout)

    try:
        config, _ = run(["ssh", "-G", "h20-train"])
        # Keep the effective endpoint, not the full machine-specific SSH configuration.
        report["commands"].pop()
        config_values = dict(line.split(" ", 1) for line in config.stdout.splitlines() if " " in line)
        for key, expected in [("hostname", "192.0.2.27"), ("port", "6622"), ("user", "user")]:
            if config_values.get(key) != expected:
                raise RuntimeError(f"Unexpected alias {key}: {config_values.get(key)}")
        report["endpoint"] = {key: config_values[key] for key in ("hostname", "port", "user")}
        preflight = remote_py(f"""import json, os, pathlib, shutil, socket, getpass
p = pathlib.Path({remote!r})
assert getpass.getuser() == 'user'
assert shutil.which('tmux') and shutil.which('rsync')
assert os.path.ismount('/u01'), '/u01 must be mounted before writing test payloads'
assert shutil.disk_usage(p.parent).free > 3 * 1024 ** 3
p.mkdir(mode=0o700)
print(json.dumps({{'user': getpass.getuser(), 'hostname': socket.gethostname(),
                  'uid': os.getuid(), 'gid': os.getgid(), 'free_bytes': shutil.disk_usage(p).free}}))
""")
        remote_created = True
        report["remote_preflight"] = json.loads(preflight.stdout)
        if shutil.disk_usage(local).free < 4 * 1024 ** 3:
            raise RuntimeError("Insufficient local temporary disk space")
        run(ssh + ["rsync --version; tmux -V; findmnt -n -o TARGET,SOURCE,FSTYPE -T /u01"])
        run(["rsync", "--version"])

        job_code = f"""import json, os, time
from pathlib import Path
from datetime import datetime, timezone
p = Path({remote!r})
with (p / 'heartbeat.jsonl').open('x') as f:
    for tick in range(23):
        f.write(json.dumps({{'tick': tick, 'pid': os.getpid(), 'utc': datetime.now(timezone.utc).isoformat()}}) + '\\n')
        f.flush()
        time.sleep(2)
(p / 'done.json').write_text(json.dumps({{'result': 'PASS', 'ticks': 23}}))
"""
        remote_py(f"from pathlib import Path; Path({remote + '/heartbeat.py'!r}).write_text({job_code!r})")
        job_command = f"exec python3 {shlex.quote(remote + '/heartbeat.py')} > {shlex.quote(remote + '/job.log')} 2>&1"
        start_command = shlex.join(["tmux", "-L", socket_name, "-f", "/dev/null", "new-session", "-d", "-s", "probe", job_command])
        started, _ = run(ssh + [start_command])
        report["heartbeat"] = {"launch_ssh_returncode": started.returncode, "socket_name": socket_name}
        time.sleep(2)
        first = heartbeat_state()
        time.sleep(6)
        second = heartbeat_state()
        report["heartbeat"].update(first_after_disconnect=first, second_after_disconnect=second)
        if not (second["ticks"] > first["ticks"] > 0 and not second["complete"]):
            raise RuntimeError("Heartbeat did not grow across independent SSH connections")
        print("PASS: heartbeat grew after launch SSH exited", flush=True)

        payload = local / "throughput.bin"
        payload_sha = random_file(payload, 1024)
        rsync = ["rsync", "-a", "--partial", "--partial-dir=.rsync-partial", "--no-whole-file", "--stats", "-e", rsh]
        upload, up_seconds = run(rsync + [str(payload), f"h20-train:{remote}/"])
        remote_digest = remote_sha(payload.name)
        downloaded = local / "downloaded.bin"
        download, down_seconds = run(rsync + [f"h20-train:{remote}/{payload.name}", str(downloaded)])
        download_digest = sha(downloaded)
        report["throughput"] = {
            "bytes_each_direction": payload.stat().st_size,
            "upload_seconds": up_seconds, "upload_mib_per_second": 1024 / up_seconds,
            "download_seconds": down_seconds, "download_mib_per_second": 1024 / down_seconds,
            "source_sha256": payload_sha, "remote_sha256": remote_digest, "download_sha256": download_digest,
            "ssh_compression": False, "rsync_compression": False,
            "timing_includes": "SSH startup, rsync transfer and process exit; excludes generation and SHA256",
        }
        if len({payload_sha, remote_digest, download_digest}) != 1:
            raise RuntimeError("Throughput file integrity mismatch")
        print(f"PASS: 1 GiB each way; upload {1024/up_seconds:.2f}, download {1024/down_seconds:.2f} MiB/s", flush=True)

        resume = local / "resume.bin"
        resume_sha = random_file(resume, 256)
        interrupted_argv = rsync + ["--bwlimit=8192", str(resume), f"h20-train:{remote}/"]
        proc = subprocess.Popen(interrupted_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
        interrupted_process = proc
        time.sleep(8)
        if proc.poll() is not None:
            raise RuntimeError("Limited transfer ended before deliberate interruption")
        proc.send_signal(signal.SIGINT)  # Only this test's sender; let receiver save partial data.
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stop_process_group(proc)
            raise RuntimeError("Interrupted test did not settle")
        interrupted_process = None
        report["commands"].append({"argv": interrupted_argv, "returncode": proc.returncode,
                                   "stdout": out, "stderr": err, "deliberate_sigint_after_seconds": 8})
        partial = json.loads(remote_py(f"""import json
from pathlib import Path
p = Path({remote!r})
f = p / '.rsync-partial/resume.bin'
print(json.dumps({{'partial_bytes': f.stat().st_size if f.exists() else 0,
                  'final_exists': (p / 'resume.bin').exists()}}))
""").stdout)
        report["resume"] = {"interrupted_returncode": proc.returncode, **partial}
        if not (proc.returncode != 0 and 0 < partial["partial_bytes"] < resume.stat().st_size and not partial["final_exists"]):
            raise RuntimeError("No valid incomplete partial file remained")
        resumed, seconds = run(rsync + [str(resume), f"h20-train:{remote}/"])
        matched = int(re.search(r"Matched data: ([\d,]+) bytes", resumed.stdout).group(1).replace(",", ""))
        literal = int(re.search(r"Literal data: ([\d,]+) bytes", resumed.stdout).group(1).replace(",", ""))
        resume_remote_sha = remote_sha(resume.name)
        report["resume"].update(bytes=resume.stat().st_size, matched_bytes=matched, literal_bytes=literal,
                                 resumed_seconds=seconds, source_sha256=resume_sha, remote_sha256=resume_remote_sha)
        if not (matched > 0 and literal < resume.stat().st_size and matched + literal == resume.stat().st_size and resume_sha == resume_remote_sha):
            raise RuntimeError("Resume did not reuse partial bytes or failed integrity check")
        print(f"PASS: interrupted transfer reused {matched} bytes; SHA256 matches", flush=True)

        deadline = time.monotonic() + 60
        final_state = heartbeat_state()
        while not final_state["complete"] and time.monotonic() < deadline:
            time.sleep(3)
            final_state = heartbeat_state()
        report["heartbeat"]["final"] = final_state
        final_files = json.loads(remote_py(f"""import json
from pathlib import Path
p = Path({remote!r})
print(json.dumps({{'done': json.loads((p / 'done.json').read_text()) if (p / 'done.json').exists() else None,
                  'heartbeat': (p / 'heartbeat.jsonl').read_text(), 'job_log': (p / 'job.log').read_text()}}))
""").stdout)
        report["heartbeat"].update(final_files)
        if not final_state["complete"] or final_files["done"] != {"result": "PASS", "ticks": 23} or final_files["job_log"]:
            raise RuntimeError("Heartbeat task did not complete cleanly")
        report["result"] = "PASS"
        print("PASS: detached job completed normally", flush=True)
    except Exception as exc:
        report["error"] = str(exc)
        print(f"FAIL: {exc}", flush=True)
    finally:
        report["cleanup"] = {"remote": False, "local": False}
        report["cleanup_errors"] = []
        if interrupted_process is not None:
            try:
                stop_process_group(interrupted_process)
            except Exception as exc:
                report["cleanup_errors"].append(str(exc))
        try:
            if remote_created:
                # Dedicated socket, never the user's normal tmux server or any training session.
                stopped, _ = run(ssh + [shlex.join(["tmux", "-L", socket_name, "kill-server"])], check=False)
                safe_to_clean = stopped.returncode == 0 or (stopped.returncode == 1 and
                    any(s in stopped.stderr for s in ("no server running", "No such file or directory")))
                # On failure, preserve the unique remote directory for diagnosis. In particular,
                # do not race remote receivers after an interrupted/timeout connection.
                if safe_to_clean and report["result"] == "PASS":
                    cleaned = remote_py(f"""from pathlib import Path
import shutil
p = Path({remote!r})
assert p.parent == Path('/u01/user/staging')
assert p.name == {'connect-acceptance-' + run_id!r} and not p.is_symlink()
shutil.rmtree(p)
print('CLEANED' if not p.exists() else 'FAILED')
""", check=False)
                    report["cleanup"]["remote"] = cleaned.returncode == 0 and cleaned.stdout.strip() == "CLEANED"
        except Exception as exc:
            report["cleanup_errors"].append(str(exc))
        try:
            shutil.rmtree(local)
            report["cleanup"]["local"] = not local.exists()
        except Exception as exc:
            report["cleanup_errors"].append(str(exc))
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(f"Receipt: {args.output}; cleanup={report['cleanup']}", flush=True)
    return 0 if report["result"] == "PASS" and all(report["cleanup"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
