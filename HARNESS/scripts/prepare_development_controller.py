#!/usr/bin/env python3
"""Copy a new Harness site and bind unchanged, previously accepted F1 controller settings."""
import argparse
import json
from pathlib import Path

from f1_harness.controller_reuse import prepare_controller


def main():
    root = Path(__file__).resolve().parents[2]
    evidence = root / 'tasks/F1_ComplexyToys/inference/artifacts/20261103-voice-V47'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_site', type=Path)
    parser.add_argument('--output-site', type=Path, required=True)
    parser.add_argument('--robot-site', required=True)
    parser.add_argument('--bundle-archive', type=Path, required=True)
    parser.add_argument('--native-preparation', type=Path, required=True)
    parser.add_argument('--source-archive', type=Path, default=evidence / 'assembly.tar.gz')
    parser.add_argument('--activation-receipt', type=Path, default=evidence / 'post-activation.json')
    args = parser.parse_args()
    result = prepare_controller(args.source_site, args.output_site, robot_site=args.robot_site,
        bundle_archive=args.bundle_archive, source_archive=args.source_archive,
        activation_receipt=args.activation_receipt, native_preparation=args.native_preparation)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
