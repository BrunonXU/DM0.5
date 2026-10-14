#!/usr/bin/env python3
"""Stage explicit sender dependencies in a separate directory; no zip/install."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    files = ["scripts/" + name for name in (
        "windows_tunnel.cmd", "windows_sender.cmd", "03_windows_shadow_sender.py", "windows_hil_shadow_sender.cmd", "windows_hil_shadow_tunnel.cmd")]
    files += ["src/g2dagger/" + name + ".py" for name in (
        "__init__", "f710_mapping", "gamepad_windows", "async_transport", "protocol", "sender_guidance", "paths")]
    files += ["src/dagger_core/" + name + ".py" for name in (
        "__init__", "f710_mapping", "gamepad_windows", "async_transport", "protocol")]
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for relative in files:
        source = root / relative
        if relative.startswith("src/dagger_core/") and not source.is_file():
            source = root.parent / "shared" / relative
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    (args.output / "manifest.json").write_text(json.dumps(dict(
        purpose="isolated Windows sender for DAgger operator transport; output determined by G2 receiver",
        files_sha256=manifest), indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
