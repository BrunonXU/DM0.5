#!/usr/bin/env python3
"""Soak real F1 observations and recording; synthetic intent, zero robot output.

Run in the F1 ROS environment. This launches only the explicit shadow entry
point on a private port and verifies socket ownership before sending intent.
Evidence is retained in a new runtime directory, including on failure.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
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

from dagger_core.protocol import IntentPacket, decode_ack, encode_packet
from f1dagger.recording import audit, read_rows


def owns_listener(pid, port):
    inodes = set()
    for path in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            continue
        if target.startswith('socket:['):
            inodes.add(target[8:-1])
    rows = [line.split() for line in Path('/proc/net/tcp').read_text().splitlines()[1:]]
    return any(row[1] == f'0100007F:{port:04X}' and row[3] == '0A' and row[9] in inodes for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--port', type=int, default=19398)
    parser.add_argument('--waiting-seconds', type=float, default=75)
    parser.add_argument('--connected-seconds', type=float, default=120)
    parser.add_argument('--minimum-capture-hz', type=float, default=28.5)
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or args.port == 7906
            or not all(math.isfinite(x) for x in (args.waiting_seconds, args.connected_seconds, args.minimum_capture_hz))
            or min(args.waiting_seconds, args.connected_seconds) < 60
            or not 0 < args.minimum_capture_hz <= 100):
        parser.error('use a dedicated port and at least 60 seconds per soak phase')
    runtime = args.runtime.resolve()
    if Path('/home/user/Bruno') not in runtime.parents or runtime.exists():
        parser.error('use a new runtime beneath /home/user/Bruno')
    runtime.mkdir(parents=True)
    command = ['bash', str(ROOT / 'scripts/50_f1_hil.sh'), 'shadow', '--purpose', 'input_check', '--port', str(args.port),
               '--runtime', str(runtime), '--task', 'SYNTHETIC INTENT / REAL CAMERAS: soak, not expert corrections']
    report = dict(ok=False, synthetic_intent=True, execution_mode='shadow', command=command,
                  waiting_seconds=args.waiting_seconds, connected_seconds=args.connected_seconds)
    log = (runtime / 'console.txt').open('x')
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    rtts, reasons = [], set()
    try:
        deadline = time.monotonic() + 45
        marker = f'F1 SHADOW (shadow): 127.0.0.1:{args.port}; waiting for START'
        while marker not in (runtime / 'console.txt').read_text():
            if process.poll() is not None:
                raise RuntimeError('shadow exited before listening; inspect console.txt')
            if time.monotonic() > deadline:
                raise TimeoutError('shadow initialization timeout')
            time.sleep(.1)
        if not owns_listener(process.pid, args.port):
            raise RuntimeError('private listener is not owned by the launched shadow process')
        print('SOAK waiting for START with continuous recording', flush=True)
        deadline = time.monotonic() + args.waiting_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('shadow died during pre-START recording')
            time.sleep(.25)
        for phase, seconds in ((0, args.connected_seconds), (1, 3.0)):
            if not owns_listener(process.pid, args.port):
                raise RuntimeError('shadow socket ownership changed')
            session = 'synthetic-soak-' + uuid.uuid4().hex
            print(f'SOAK connected phase={phase} duration={seconds}', flush=True)
            with socket.create_connection(('127.0.0.1', args.port), timeout=.5) as client:
                stream = client.makefile('rb')
                started = time.monotonic()
                seq = 0
                while time.monotonic() - started < seconds:
                    tick = time.monotonic()
                    # Fresh release then START in each session; no auto-resume.
                    active = phase == 0 and (150 <= seq < 200 or 350 <= seq < 400)
                    packet = IntentPacket(session_id=session, sequence=seq, sent_at_unix_ns=time.time_ns(),
                        deadman=active, active_side='left' if seq < 250 else 'right', mode='translation',
                        axes=(.05, 0., 0.) if active else (0., 0., 0.), grip_velocity=0.,
                        thumb_clearance=False, hil_toggle_id=int(seq >= 25), hil_button_pressed=seq == 25)
                    client.sendall(encode_packet(packet))
                    line = stream.readline()
                    if not line:
                        raise RuntimeError('shadow receiver closed the connection')
                    ack = decode_ack(line)
                    if (ack.session_id, ack.sequence) != (session, seq):
                        raise RuntimeError('ACK identity mismatch: ' + str(ack))
                    if not ack.accepted:
                        raise RuntimeError('intent rejected: ' + str(ack))
                    if not (ack.reason or '').endswith(';execution_mode=shadow'):
                        raise RuntimeError('receiver did not identify itself as SHADOW: ' + str(ack))
                    rtts.append((time.monotonic() - tick) * 1000)
                    reasons.add(ack.reason)
                    seq += 1
                    time.sleep(max(0., .02 - (time.monotonic() - tick)))
                stream.close()
            time.sleep(.4)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            report['exit_code'] = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            report['error'] = 'shadow cleanup timed out; process retained, inspect console.txt'
            report['process_pid'] = process.pid
        log.close()
    report.update(ack_count=len(rtts), ack_reasons=sorted(reasons, key=str),
                  ack_rtt_ms_max=max(rtts, default=None))
    try:
        runs = list((runtime / 'raw').glob('*'))
        if len(runs) != 1:
            raise RuntimeError('expected one raw run')
        run = runs[0]
        report['run'] = str(run)
        if (run / 'camera_diagnostics.json').exists():
            report['camera_diagnostics'] = json.loads((run / 'camera_diagnostics.json').read_text())
        report['audit'] = audit(run)
        binding = json.loads((run / 'journal/manifest.json').read_text())['binding']
        if binding.get('execution_mode') != 'shadow':
            raise RuntimeError('run journal is not bound to shadow execution')
        observations = read_rows(run / 'journal/observations.jsonl')
        controls = read_rows(run / 'journal/control.jsonl')
        report['payload_manifest'] = json.loads((run / 'payload/manifest.json').read_text())
        report['capture_hz_measured'] = (len(observations) - 1) * 1e9 / (
            observations[-1]['available_monotonic_ns'] - observations[0]['available_monotonic_ns'])
        ages = sorted((row['available_monotonic_ns'] - f['host_received_monotonic_ns']) / 1e6
                      for row in observations for f in row['frames'])
        report['camera_age_ms'] = dict(median=ages[len(ages)//2], p99=ages[int((len(ages)-1)*.99)], maximum=ages[-1])
        report['camera_dimensions'] = sorted({(f['camera'], f['width'], f['height'])
            for row in observations for f in row['frames']})
        report['controls'] = len(controls)
        report['sources'] = sorted({r['source'] for r in controls})
        report['states'] = sorted({r['hil_state'] for r in controls})
        report['any_submitted_action'] = any(r['submitted'] is not None or r.get('publications') for r in controls)
        report['ok'] = (not report.get('error') and report.get('exit_code') == 0
            and report['audit']['valid'] and not report['any_submitted_action']
            and report['capture_hz_measured'] >= args.minimum_capture_hz
            and 'human_arm' in report['sources'] and 'safe_hold' in report['states']
            and all((w, h) == (640, 480) for name, w, h in report['camera_dimensions'] if name != 'head'))
    except Exception as exc:
        report['audit_error'] = f'{type(exc).__name__}: {exc}'
    (runtime / 'soak_report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
