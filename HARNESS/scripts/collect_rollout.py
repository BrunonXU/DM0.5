#!/usr/bin/env python3
"""Collect a stopped F1 Harness owner run bound to one planner run; optionally seal it."""
import argparse
import json
from pathlib import Path

from f1_harness.collect import REMOTE_RUNTIME, collect_rollout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('planner_run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--host', default='user@127.0.0.1')
    parser.add_argument('--port', type=int, default=22009)
    parser.add_argument('--identity', type=Path, default=Path('/home/user/.ssh/id_ed25519_f1'))
    parser.add_argument('--known-hosts', type=Path, default=Path(__file__).resolve().parents[1] / '.cache/robot_known_hosts')
    parser.add_argument('--remote-runtime', default=REMOTE_RUNTIME)
    parser.add_argument('--max-gib', type=int, default=16)
    parser.add_argument('--seal', action='store_true')
    parser.add_argument('--render-video', action='store_true')
    args = parser.parse_args()
    result = collect_rollout(args.planner_run, args.output, host=args.host, port=args.port,
        identity=args.identity, known_hosts=args.known_hosts, remote_runtime=args.remote_runtime,
        max_bytes=args.max_gib * 1024**3, seal=args.seal, render_video=args.render_video)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get('seal', {}).get('integrity_complete') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
