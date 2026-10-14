#!/usr/bin/env python3
"""Inspect F710 motion replay evidence offline. Execution uses 4242's lease UI."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.replay_data import read_recording
from robot.replay_action import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('episode', type=Path)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--end', type=float)
    parser.add_argument('--profile', type=Path, help='Optional current robot profile for static trajectory checks')
    args = parser.parse_args()
    try:
        segment = read_recording(args.episode).select(args.start, args.end)
        result = segment.summary()
        if args.profile:
            import hashlib
            profile = json.loads(args.profile.read_text())
            profile['profile_sha256'] = hashlib.sha256(args.profile.read_bytes()).hexdigest()
            result['metrics'] = validate(profile, segment)
        result.update(motion_started=False, live_start_checked=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
