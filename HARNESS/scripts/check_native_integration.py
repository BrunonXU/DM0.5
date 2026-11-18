"""Exercise the native WebHarness lifecycle with fake transport and no ROS endpoints.

HARNESS's default pytest discovery does not include DAgger's real control loop.
Run this entry alongside the HARNESS suite after changing owner/control wiring.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--junitxml', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dagger = root.parent / 'DAgger'
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(str(p) for p in (
        root / 'src', dagger / 'F1/src', dagger / 'shared/src',
    ))
    with tempfile.TemporaryDirectory(prefix='harness-native-pytest-') as cache:
        command = [sys.executable, '-m', 'pytest', '-q', '-o', f'cache_dir={cache}',
                   'F1/tests/test_f1_web_timing_recovery.py',
                   'F1/tests/test_f1_harness.py', 'F1/tests/test_f1_output.py']
        if args.junitxml:
            command += ['--junitxml', str(args.junitxml.resolve())]
        return subprocess.run(command, cwd=dagger, env=env, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
