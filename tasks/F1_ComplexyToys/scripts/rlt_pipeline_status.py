#!/usr/bin/env python3
"""Read actual admission, learning and candidate receipts without robot I/O."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_admission import admission_summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    args = p.parse_args()
    cfg = json.loads(args.config.read_text())['f1']
    out = admission_summary(cfg['health_root'])
    for key, path in [('learner', Path(cfg['health_root'])/'actor.json'),
                      ('latest_candidate', Path(cfg['snapshot_root'])/'latest.json')]:
        out[key] = json.loads(path.read_text()) if path.exists() else None
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
