#!/usr/bin/env python3
"""Build a pinned PyRUA overlay; original upstream checkout is never modified."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build():
    manifest = json.loads((ROOT / "configs/upstreams.lock.json").read_text())
    entry = next(row for row in manifest["repositories"] if row["name"] == "PyRUA-Lean")
    checkout = ROOT / entry["path"]
    actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if actual != entry["commit"]:
        raise RuntimeError("PyRUA pin mismatch")
    source = checkout / "src/pyrualean"
    target = ROOT / ".overlay/pyrualean"
    target.parent.mkdir(exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    registry = target / "robots.py"
    text = registry.read_text()
    text = text.replace('ROBOTS: tuple[str, ...] = (', 'ROBOTS: tuple[str, ...] = (\n    "f1",', 1)
    anchor = '    if name not in ROBOTS:'
    assert anchor in text
    text = text.replace(anchor, '    if name == "f1":\n        from f1_harness.adapters import ADAPTER\n        return ADAPTER\n' + anchor, 1)
    registry.write_text(text)
    prompt = target / "prompt.py"
    text = prompt.read_text()
    anchor = '    stem = knowledge_stem(name, primitives)'
    assert anchor in text
    text = text.replace(anchor, '    if name == "f1":\n        return resources.files("f1_harness").joinpath("knowledge/f1.md").read_text(encoding="utf-8")\n' + anchor, 1)
    prompt.write_text(text)
    receipt = dict(upstream_commit=actual, files={str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                                                 for p in (registry, prompt)})
    (target.parent / "manifest.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    build()
