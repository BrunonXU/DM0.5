#!/usr/bin/env python3
"""Select the next local task's trial or baseline actor; never send an action."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))


def main():
    from f1_toys.rlt_online import sha, atomic_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--channel', choices=('latest', 'baseline'), required=True)
    args = parser.parse_args()
    pointer = json.loads((args.registry/(args.channel+'.json')).read_text())
    name = pointer['directory']
    source = args.registry/name
    path = source.resolve()
    if Path(name).name != name or path.parent != args.registry.resolve() or source.is_symlink():
        raise ValueError('Invalid registry pointer')
    if sha(path/'manifest.json') != pointer['manifest_sha256']:
        raise ValueError('Actor manifest changed')
    manifest = json.loads((path/'manifest.json').read_text())
    if manifest.get('deployment_approved') is not True or sha(path/'head.safetensors') != manifest['head_sha256']:
        raise ValueError('Actor is not a validated complete snapshot')
    atomic_json(args.registry/'selection.json', dict(channel=args.channel))
    print(json.dumps(dict(channel=args.channel, snapshot=name, effect='next_local_task_only', robot_commands=0)))


if __name__ == '__main__':
    main()
