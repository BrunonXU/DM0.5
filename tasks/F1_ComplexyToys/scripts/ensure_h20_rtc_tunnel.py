"""F1 H20 inference link supervisor; no robot imports or control output."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path("/home/user/Bruno/DAgger-runtime/f1-toys-20261016")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", choices=("5000", "50000"))
    parser.add_argument("--supervise", action="store_true")
    args = parser.parse_args()
    profiles = json.loads((ROOT / "h20-rtc/profiles.json").read_text())
    profile = profiles[args.checkpoint]
    runtime = ROOT / "h20-rtc" / args.checkpoint
    runtime.mkdir(parents=True, exist_ok=True)
    keys = ROOT / "h20-ab"
    command = [
        "ssh", "-NT", "-p", "6622", "-i", str(keys / "id_ed25519"),
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(keys / "known_hosts"),
        "-o", "ControlMaster=no", "-o", "ControlPath=none",
        "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{profile['local_port']}:127.0.0.1:{profile['remote_port']}",
        "user@192.0.2.27",
    ]

    def health():
        with urlopen(f"http://127.0.0.1:{profile['local_port']}/health", timeout=2) as stream:
            result = json.load(stream)
        expected = dict(ready=True, protocol="f1_eef_rtc_v2",
                        model_id=profile["model_id"], model_version=profile["model_version"],
                        prediction_horizon=50, denoise_steps=5, rtc=True)
        for key, value in expected.items():
            if result.get(key) != value:
                raise RuntimeError(f"H20 {args.checkpoint}: health mismatch for {key}")
        return result

    if args.supervise:
        with (runtime / "supervisor.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            (runtime / "supervisor.pid").write_text(str(os.getpid()))
            while True:
                subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
                time.sleep(2)
    else:
        # Always establish supervisor ownership, even if an existing link is healthy.
        with (runtime / "tunnel.log").open("ab") as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                              args.checkpoint, "--supervise"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
        failure = None
        for _ in range(15):
            try:
                health()
                print(f"H20 RTC {args.checkpoint} ready: F1 {profile['local_port']} "
                      f"-> H20 {profile['remote_port']}; {profile['model_id']}", flush=True)
                return
            except Exception as exc:
                failure = exc
                time.sleep(1)
        raise RuntimeError(f"H20 RTC unavailable: {failure}; inspect {runtime/'tunnel.log'}")


if __name__ == "__main__":
    main()
