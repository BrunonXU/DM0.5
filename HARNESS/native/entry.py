"""Run with the existing F1 native interpreter after sourcing ROS Jazzy setup.bash."""
from pathlib import Path
import hashlib
import json
import sys
import time
entry_started = time.monotonic()
root=Path(__file__).resolve().parent.parent
manifest_path=root/'manifest.json'
if manifest_path.is_file():
    manifest=json.loads(manifest_path.read_text());hashes=manifest['files']
    if hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()!=manifest['bundle_id']:
        raise ValueError('bundle_manifest_identity_changed')
    for relative,expected in hashes.items():
        # Installed wheel dependencies were checked at install time. Executable
        # source and configuration are checked on each native process startup.
        if relative.startswith(('src/','control/','native/','upstream/','configs/')):
            path=root/relative
            if (not path.resolve().is_relative_to(root.resolve()) or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest()!=expected):
                raise ValueError('bundle_source_integrity_failure:'+relative)
# Source ownership is explicit: both numerical and owner processes use this
# bundle's F1/core snapshot, never an older production source from PYTHONPATH.
sys.path[:0]=[str(root/'deps'),str(root/'src'),str(root/'control/src'),str(root/'upstream/RPent')]
integrity_finished = time.monotonic()
from f1_harness.native import main
if __name__ == '__main__':
    sys._f1_native_entry_timing = dict(started=entry_started,
        source_integrity_s=integrity_finished-entry_started,
        native_import_s=time.monotonic()-integrity_finished)
    raise SystemExit(main())
