#!/usr/bin/env python3
"""Offline fresh-frame cadence audit. Does not load GDK."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

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
