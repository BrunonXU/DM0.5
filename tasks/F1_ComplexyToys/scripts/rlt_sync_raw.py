#!/usr/bin/env python3
"""Resumable raw mirror; separate process, never runs on the control thread."""
import argparse
from pathlib import Path
import subprocess
import shlex
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', required=True, help='SSH alias:absolute/path or local absolute directory')
    parser.add_argument('--pull-source', help='Optional F1 SSH alias:absolute/raw/path; mirror via --source on 6000D')
    parser.add_argument('--pull-ssh-config', type=Path)
    parser.add_argument('--local-segments-only', action='store_true', help='Mirror only episodes with a durable local-task receipt')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.pull_source:
        if ':' not in args.pull_source or args.pull_source.startswith('-'):
            parser.error('Pull source must be an SSH alias:absolute/path')
        args.source.mkdir(parents=True, exist_ok=True)
    if not args.source.is_dir() or args.destination.startswith('-'):
        parser.error('Existing source directory and non-option destination required')
    # No --delete, no in-place rewrite: transferred prefix receipts verify exact
    # byte ranges before consumption. A failed transfer cannot enter replay.
    while True:
        if args.pull_source:
            ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
            if args.pull_ssh_config:
                ssh += ['-F', str(args.pull_ssh_config.resolve())]
            base = ['rsync', '-a', '--partial', '--protect-args', '-e', shlex.join(ssh)]
            if args.local_segments_only:
                pull = subprocess.run([*base, '--include=*/', '--include=placement-1.json', '--exclude=*',
                    '--', args.pull_source.rstrip('/')+'/', str(args.source.resolve())+'/'])
                names = sorted({p.parent.parent.name for p in args.source.glob('*/local_segments/placement-1.json')})
                listing = b''.join(name.encode()+b'/\0' for name in names)
                if names:
                    pull = subprocess.run([*base, '--recursive', '--from0', '--files-from=-', '--',
                        args.pull_source.rstrip('/')+'/', str(args.source.resolve())+'/'], input=listing)
            else:
                pull = subprocess.run([*base, '--', args.pull_source.rstrip('/')+'/', str(args.source.resolve())+'/'])
            # Previously mirrored complete prefixes remain useful if F1 goes
            # offline; the upload side can keep draining them independently.
        selection = []
        listing = None
        if args.local_segments_only:
            names = sorted({p.parent.parent.name for p in args.source.glob('*/local_segments/placement-1.json')})
            listing = b''.join(name.encode()+b'/\0' for name in names)
            selection = ['--recursive', '--from0', '--files-from=-']
        result = subprocess.run(['rsync', '-a', '--partial', '--protect-args',
            '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=10', *selection, '--',
            str(args.source.resolve())+'/', args.destination.rstrip('/')+'/'], input=listing)
        if args.once:
            return result.returncode or (pull.returncode if args.pull_source else 0)
        time.sleep(2. if result.returncode == 0 else 10.)


if __name__ == '__main__':
    raise SystemExit(main())
