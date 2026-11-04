"""F1-only installation of already validated H20 RTC entries; no robot control."""
import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
from urllib.request import urlopen

ROOT = Path("/home/user/Bruno/DAgger-runtime/f1-toys-20261016")
ENTRY = Path("/home/user/Bruno/DAgger")
OPTIMIZATION = "compiled_suffix_precision_cudagraphs_v1_torch27_tuple_compat"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    profiles = json.loads((ROOT / "h20-rtc/profiles.json").read_text())
    health = {}
    for step, profile in profiles.items():
        with urlopen(f"http://127.0.0.1:{profile['local_port']}/health", timeout=5) as response:
            health[step] = json.load(response)
        for key, expected in dict(ready=True, model_id=profile["model_id"],
                                  model_version=profile["model_version"],
                                  protocol="f1_eef_rtc_v2", denoise_steps=5,
                                  prediction_horizon=50, rtc_optimization=OPTIMIZATION).items():
            assert health[step][key] == expected, (step, key)
        config = json.loads((ROOT / f"h20-rtc/config-{step}.json").read_text())
        assert config["policy"]["model_id"] == profile["model_id"]
        assert config["policy"]["endpoint"] == f"http://127.0.0.1:{profile['local_port']}"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with open("/home/user/Bruno/F1/runtime/control.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        default = ENTRY / "start_f1_toys_rtc.sh"
        backup = ENTRY / f"start_f1_toys_rtc.before-h20-{stamp}.sh"
        shutil.copy2(default, backup)
        fallback = ENTRY / "start_f1_toys_rtc_6000d.sh"
        if not fallback.exists():
            assert "ensure_rtc_tunnel.py" in default.read_text(), "Original 6000D entry missing"
            shutil.copy2(default, fallback)
        files = {}
        for name, step in (("start_f1_toys_rtc.sh", "5000"),
                           ("start_f1_toys_rtc_50000.sh", "50000")):
            path = ENTRY / name
            temporary = path.with_name(name + ".h20-new")
            temporary.write_text("#!/usr/bin/env bash\nset -e\n"
                                 f'exec bash "{ENTRY}/start_f1_toys_rtc_h20.sh" {step} "$@"\n')
            temporary.chmod(0o755)
            temporary.replace(path)
            files[str(path)] = digest(path)
        files[str(fallback)] = digest(fallback)
        for path in (ENTRY / "start_f1_toys_rtc_h20.sh", ROOT / "ensure_h20_rtc_tunnel.py"):
            files[str(path)] = digest(path)
        receipt = dict(installed_utc=stamp, health=health, files_sha256=files,
                       previous_entry=str(backup), robot_commands=0,
                       default_checkpoint=5000, alternate_checkpoint=50000,
                       onsite_motion_validation="pending",
                       limitations=["Cross-host gripper transitions are not numerically equivalent.",
                                    "50K stationary shadow has IK rejections; not a success-rate ranking."])
        target = ROOT / f"h20-rtc/deployment-{stamp}.json"
        target.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
