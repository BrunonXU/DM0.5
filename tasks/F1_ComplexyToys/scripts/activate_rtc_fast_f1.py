"""Activate the tested pixel client while holding the F1 control lock; no motion."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path("/home/user/Bruno/DAgger")
    runtime = Path("/home/user/Bruno/DAgger-runtime/f1-toys-20261016/h20-rtc")
    candidate = runtime / "fast-candidate"
    staged = json.loads((candidate / "staging.json").read_text())
    release = Path(staged["release"])
    with open("/home/user/Bruno/F1/runtime/control.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = release / "F1_RELEASE_MANIFEST.json"
        assert sha(manifest_path) == staged["manifest_sha256"]
        for name, digest in json.loads(manifest_path.read_text())["files"].items():
            assert sha(release / name) == digest, name
        health = {}
        summaries = {}
        for step, port in (("5000", 8147), ("50000", 8148)):
            config = json.loads((candidate / f"config-{step}.json").read_text())
            assert config["accepted_release_manifest_sha256"] == staged["manifest_sha256"]
            assert config["policy"]["rtc_pixel_workers"] == 3
            summary_path = runtime / f"fast-after-{step}/summary.json"
            summary = json.loads(summary_path.read_text())
            assert summary["published_records"] == summary["submitted_records"] == 0
            assert summary["requests"] > 100
            assert set(summary["execution_modes"]) == {"shadow"}
            assert summary["events"].get("rtc_timing_paused", 0) == 0
            assert summary["events"].get("standalone_finished") == 1
            summaries[step] = {"path": str(summary_path), "sha256": sha(summary_path)}
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
                health[step] = json.load(response)
            expected_version = {
                "5000": "ff1263050a8b684686fba44022e4075cafc37eb6997b5d3cbbba912685e4da98",
                "50000": "1d55a3e0017742b05750ecab6bfc3f73486b8cfd73d1dd13f6844079fd785b47",
            }[step]
            assert health[step]["ready"]
            assert health[step]["model_id"] == config["policy"]["model_id"]
            assert health[step]["model_version"] == expected_version
            assert health[step]["rtc_optimization"].endswith("_native_math_v1")
        entry = root / "start_f1_toys_rtc_h20.sh"
        backup = entry.with_name(entry.name + ".before-fast-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        shutil.copy2(entry, backup)
        temporary = entry.with_name(entry.name + ".fast-tmp")
        shutil.copy2(candidate / "start.sh", temporary)
        temporary.chmod(entry.stat().st_mode)
        os.replace(temporary, entry)
        receipt = {
            "entry": str(entry), "sha256": sha(entry), "backup": str(backup),
            "backup_sha256": sha(backup), "release": str(release),
            "manifest_sha256": staged["manifest_sha256"], "health": health,
            "shadow_summaries": summaries, "robot_commands": 0,
        }
        (candidate / "activation.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
