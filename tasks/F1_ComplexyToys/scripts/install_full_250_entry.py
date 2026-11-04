"""F1-side installer for Full 250 configuration; no robot imports or commands."""
import fcntl
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, content):
    if path.exists():
        assert path.read_bytes() == content, f"Refuse overwrite: {path}"
    else:
        with path.open("xb") as stream:
            stream.write(content)


def main():
    root = Path("/home/user/Bruno/DAgger-runtime/f1-toys-20261016")
    target = root / "full-rtc/250"
    release = Path("/home/user/Bruno/DAgger/releases/20261020-f1-toys-rtc-v5-fastpixels/DAgger")
    with open("/home/user/Bruno/F1/runtime/control.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((root / "h20-rtc/fast-candidate/config-5000.json").read_text())
        manifest = release / "F1_RELEASE_MANIFEST.json"
        assert sha(manifest) == config["accepted_release_manifest_sha256"]
        for name, digest in json.loads(manifest.read_text())["files"].items():
            assert sha(release/name) == digest, name
        profile = json.loads((target / "profile.json").read_text())
        assert sha(target/"processing.py") == profile["input_preprocessing"]["implementation_sha256"]
        assert sha(target/"contract.json") == profile["input_preprocessing"]["contract_sha256"]
        prompt = json.loads((target/"contract.json").read_text())["prompt"]
        assert prompt in (target/"start_f1_toys_full_250.sh").read_text()
        config["policy"].update(endpoint="http://127.0.0.1:8151",
                                model_id=profile["model_id"],
                                rtc_pixels_directory=str(target), rtc_pixel_workers=3)
        write_new(target/"config.json", (json.dumps(config, indent=2)+"\n").encode())
        entry = Path("/home/user/Bruno/DAgger/start_f1_toys_full_250.sh")
        write_new(entry, (target/"start_f1_toys_full_250.sh").read_bytes())
        entry.chmod(0o755)
        receipt = dict(entry=str(entry), config=str(target/"config.json"),
                       release=str(release), release_sha256=sha(manifest),
                       model_id=profile["model_id"], model_version=profile["model_version"],
                       entry_sha256=sha(entry), config_sha256=sha(target/"config.json"),
                       robot_commands=0, entry_executed=False,
                       existing_entries_unchanged=True)
        write_new(target/"installation.json", (json.dumps(receipt, indent=2)+"\n").encode())
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
