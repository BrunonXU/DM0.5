#!/usr/bin/env python3
"""Offline three-camera review MP4 export; preserves raw timestamp sidecars."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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
