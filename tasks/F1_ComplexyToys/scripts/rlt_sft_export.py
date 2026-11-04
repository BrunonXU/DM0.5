#!/usr/bin/env python3
"""Independent reviewed-human SFT consumer; never updates VLA or robot policy."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

TASK = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('raw-root', 'reviews', 'baseline', 'output-root'):
        parser.add_argument('--'+key, type=Path, required=True)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    sys.path[:0] = [str(TASK/'src'), str(TASK/'scripts')]
    from prepare_hil_correction import prepare
    from f1_toys.rlt_online import atomic_json, sha
    args.output_root.mkdir(parents=True, exist_ok=True)
    seen = set()
    while True:
        for review_path in sorted(args.reviews.glob('*.json')):
            review = json.loads(review_path.read_text())
            episode = review.get('episode_id', review_path.stem)
            if Path(episode).name != episode or episode in ('.', '..'):
                raise ValueError('Review episode must be a single directory name')
            raw = args.raw_root/episode
            manifest = raw/'journal/manifest.json'
            if not manifest.exists():
                continue  # Full SFT waits for Y; local RL uses its earlier receipt.
            if review.get('human_review_accepted') is not True:
                continue
            signature = dict(source=sha(manifest), review=sha(review_path),
                baseline=sha(args.baseline/'manifest.json'),
                adapter=sha(TASK/'scripts/prepare_hil_correction.py'),
                index=sha(TASK/'DataPrehandle/build_training_index.py'))
            key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
            if key in seen:
                continue
            output = args.output_root/key
            if not output.exists():
                try:
                    receipt = prepare(raw, review_path, args.baseline, output)
                    atomic_json(output/'consumer-source.json', signature)
                except Exception as exc:
                    atomic_json(args.output_root/(key+'.error.json'), dict(episode=episode, source=signature,
                        error=str(exc), training_started=False))
                    if not args.watch:
                        raise
                    seen.add(key)
                    continue
            seen.add(key)
        if not args.watch:
            return
        time.sleep(5.)


if __name__ == '__main__':
    main()
