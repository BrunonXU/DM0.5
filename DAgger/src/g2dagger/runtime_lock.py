"""Read-only preflight for the shared Robot-owner PID file."""
import os
from pathlib import Path


def check_runtime_available(path=Path('/tmp/g2_dm05_runtime.pid')):
    """Do not remove files or signal owners. Atomic acquisition is still required."""
    try:
        pid = int(path.read_text().strip())
    except FileNotFoundError:
        return
    except ValueError as exc:
        raise RuntimeError(f'Runtime lock is incomplete/invalid: {path}; retry after the other startup finishes') from exc
    if pid <= 0:
        raise RuntimeError(f'Invalid runtime PID {pid} in {path}')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return  # Existing atomic acquisition handles the stale file.
    except PermissionError:
        pass  # Cannot prove the owner is absent.
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
    except OSError:
        command = '(command unavailable)'
    raise RuntimeError(f'G2 runtime occupied: PID {pid}: {command}. '
                       'Exit that runtime in its original terminal before starting DAgger. '
                       'Do not delete an active owner lock.')
