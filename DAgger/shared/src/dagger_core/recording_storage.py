"""Files-only capacity check before creating a hardware owner."""
from pathlib import Path
import shutil


def check_recording_storage(paths, *, minimum_free_bytes=2 * 1024**3):
    """A startup reserve, not a guarantee that an arbitrary episode will fit."""
    for path in paths:
        ancestor = Path(path).resolve()
        while not ancestor.exists():
            ancestor = ancestor.parent
        free = shutil.disk_usage(ancestor).free
        if free < minimum_free_bytes:
            raise RuntimeError(
                f'recording disk space low: {path} (volume path {ancestor}), '
                f'free={free / 1024**3:.2f} GiB, '
                f'required reserve={minimum_free_bytes / 1024**3:.2f} GiB; '
                'move recordings to a data volume before starting')
