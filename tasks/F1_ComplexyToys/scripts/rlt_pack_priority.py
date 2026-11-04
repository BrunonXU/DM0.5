#!/usr/bin/env python3
"""Pack durable local learning segments independently of whole-episode upload."""
import argparse
import json
from pathlib import Path
import sys
import time
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_priority import build_bundle, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--bundles', type=Path, required=True)
    parser.add_argument('--reviews-root', type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--not-before-utc', help='Only new episode timestamps, YYYYMMDDTHHMMSSZ')
    args = parser.parse_args()
    if args.not_before_utc and not re.fullmatch(r'\d{8}T\d{6}Z', args.not_before_utc):
        parser.error('Invalid UTC episode cutoff')
    seen = set()
    pending = {}
    while True:
        for receipt in sorted(args.source.glob('*/local_segments/placement-1.json'), reverse=True):
            try:
                data = receipt.read_bytes(); episode = json.loads(data)['episode_id']
                if args.not_before_utc:
                    match = re.fullmatch(r'f1_hil_(\d{8}T\d{6}Z)_[0-9a-f]+', episode)
                    if not match or match[1] < args.not_before_utc:continue
                review = args.reviews_root/(episode+'.json') if args.reviews_root else None
                if review and not review.exists():review = None
                revision = digest(data+(review.read_bytes() if review else b''))
                if revision in seen:continue
                start = time.monotonic()
                path = build_bundle(receipt.parent.parent, args.bundles, review_path=review)
                seen.add(revision)
                print(json.dumps(dict(status='committed',episode_id=episode,bundle=str(path),
                                      seconds=time.monotonic()-start)),flush=True)
            except (OSError, ValueError, KeyError) as exc:
                # Partial arrivals and unreviewed interruptions retry without
                # holding up valid new episodes or the robot control process.
                if pending.get(str(receipt)) != str(exc):
                    print(json.dumps(dict(status='pending',receipt=str(receipt),error=str(exc))),flush=True)
                    pending[str(receipt)] = str(exc)
        if args.once:return 0
        time.sleep(2.)


if __name__ == '__main__':
    raise SystemExit(main())
