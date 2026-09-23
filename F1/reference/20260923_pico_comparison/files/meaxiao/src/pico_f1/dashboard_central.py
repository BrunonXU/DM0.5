"""Authenticated, loopback-only dashboard over the final 6000d dataset."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys

from pico_f1.transfer import DEFAULT_DEST, lock, read


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    selection = root.parent / "transfer_6000d/dashboard_selection.json"
    default_root = Path(read(selection)["root"]) if selection.exists() else DEFAULT_DEST
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--port", type=int, default=18976)
    args = parser.parse_args()
    os.umask(0o077)
    home = root.parent / "private_review_central"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep the existing credential so Windows users do not need a new key.
    token = root.parent / "private_review_D070/access.token"
    token.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not token.exists():
        with token.open("x") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    token.chmod(0o600)
    cache_key = hashlib.sha256(str(args.root.absolute()).encode()).hexdigest()[:16]
    with lock(home / f"dashboard-{args.port}.lock", nonblock=True):
        print(f"PRIVATE_REVIEW http://127.0.0.1:{args.port}/ 数据源=6000d最终数据集 {args.root}", flush=True)
        # Invoke in process: retain singleton lock for the entire server lifetime.
        sys.argv = ["dashboard_server", "--root", str(args.root), "--cache", str(home / cache_key),
                    "--logs", str(root.parent / "transfer_6000d/logs"), "--port", str(args.port),
                    "--auth-token-file", str(token)]
        from pico_f1.dashboard_server import main as serve
        serve()


if __name__ == "__main__":
    main()
