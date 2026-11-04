"""Run an explicitly shadow-only entry, summarize complete journals, never power."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--checkpoint', choices=('5000', '50000'), required=True)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.entry.read_text()
    assert '--shadow)' in source and 'mode=sync-shadow; execute=()' in source
    assert args.seconds > 0
    args.output.mkdir(parents=True, exist_ok=False)
    command = ['bash', str(args.entry), args.checkpoint, '--shadow',
               '--duration-seconds', str(args.seconds)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
    run, resumes = None, []
    with (args.output/'console.log').open('x') as log:
        for line in process.stdout:
            log.write(line)
            log.flush()
            if 'standalone sync:' in line and 'raw=' in line:
                run = line.split('raw=', 1)[1].strip()
                print(json.dumps({'run': run, 'seconds': args.seconds}), flush=True)
            if 'RTC响应超时或计划耗尽' in line or 'IK连续拒绝' in line:
                process.stdin.write('\n')
                process.stdin.flush()
                resumes.append(dict(unix=time.time(), message=line.strip()))
    code = process.wait()
    process.stdin.close()
    receipt = dict(exit_code=code, run=run, shadow_only_simulated_enter=resumes,
                   zero_output_requested=True, command=command)
    (args.output/'runner.json').write_text(json.dumps(receipt, indent=2))
    assert code == 0 and run, receipt
    root = Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016')
    subprocess.run(['/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python',
                    str(root/'h20-rtc/summarize_rtc_shadow.py'), run, '--output',
                    str(args.output/'summary.json')], check=True)


if __name__ == '__main__':
    main()
