#!/usr/bin/env python3
"""Offline fresh-frame cadence audit. Does not load GDK."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from g2dagger.recording_rate import audit_capture_rate

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('episode_dir', type=Path)
    parser.add_argument('--target-hz', type=float, default=30.)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with (args.episode_dir / 'observations.jsonl').open() as stream:
        report = audit_capture_rate([json.loads(line) for line in stream], args.target_hz)
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end='')
    raise SystemExit(0 if report['target_rate_met'] else 2)
