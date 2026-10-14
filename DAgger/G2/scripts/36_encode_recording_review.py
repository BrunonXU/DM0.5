#!/usr/bin/env python3
"""Offline three-camera review MP4 export; preserves raw timestamp sidecars."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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

from g2dagger.recording_video import encode_review_videos


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(encode_review_videos(args.archive, args.output))
    print("Review videos decoded/count checked; training_ready=false.")


if __name__ == "__main__":
    main()
