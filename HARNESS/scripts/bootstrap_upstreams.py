#!/usr/bin/env python3
"""Fetch source pins or verify existing checkouts; never install or run upstreams."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "configs/upstreams.lock.json"


def git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        env=env, text=True, stderr=subprocess.PIPE, timeout=240,
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Create missing checkouts; refuses to overwrite existing work")
    options = parser.parse_args()
    manifest = json.loads(LOCK.read_text())
    for entry in manifest["repositories"]:
        relative = Path(entry["path"])
        target = ROOT / relative
        sha = entry["commit"]
        if (relative.is_absolute() or len(relative.parts) != 2
                or relative.parts[0] != "third_party"
                or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or not entry["url"].startswith("https://github.com/")):
            raise ValueError(f"Invalid source lock entry: {entry['name']}")
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError(f"Refusing symlink checkout: {relative}")
        if not target.exists():
            if not options.fetch:
                raise ValueError(f"Missing checkout: {relative}; run with --fetch")
            target.mkdir(parents=True)
            git(target, "init", "--quiet")
            git(target, "remote", "add", "origin", entry["url"])
        if not (target / ".git").is_dir():
            raise ValueError(f"Not an independent checkout: {relative}")
        if git(target, "remote", "get-url", "origin") != entry["url"]:
            raise ValueError(f"Origin mismatch: {relative}")
        if git(target, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(f"Dirty checkout, preserving work: {relative}")
        try:
            current = git(target, "rev-parse", "--verify", "HEAD")
        except subprocess.CalledProcessError:
            current = None
        if current is not None and current != sha:
            raise ValueError(f"Different HEAD, preserving checkout: {relative} ({current})")
        if current is None:
            if not options.fetch:
                raise ValueError(f"Incomplete checkout: {relative}; run with --fetch")
            print(f"Fetching {entry['name']} @ {sha}", flush=True)
            git(target, "fetch", "--depth=1", "--no-tags", "origin", sha)
            git(target, "checkout", "--quiet", "--detach", sha)
        actual = git(target, "rev-parse", "HEAD")
        if actual != sha or git(target, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(f"Verification failed: {relative}")
        print(json.dumps({"name": entry["name"], "commit": actual,
                          "clean": True, "path": str(relative)}), flush=True)
    print("Source pins verified. Dependencies, API and robot runtime have NOT been tested.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # No environment or credentials are included in failure output.
        print(f"Source checkout failed: {exc}", file=sys.stderr)
        sys.exit(1)
