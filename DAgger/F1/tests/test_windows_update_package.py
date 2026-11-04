"""Exercise the update in a legacy client without any shared-source checkout."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import zipfile


def test_legacy_client_update_contains_complete_import_closure(tmp_path):
    f1 = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(f1 / 'scripts/68_package_windows_input_pause.py'))['build']
    output = tmp_path / 'package'
    build(output)
    client = tmp_path / 'legacy client'
    (client / 'scripts').mkdir(parents=True)
    (client / 'src/g2teleop').mkdir(parents=True)
    launcher = client / 'scripts/windows_f1_sender.cmd'
    launcher.write_text('existing launcher')
    assert not (client / 'src/dagger_core').exists()
    archive = output / 'windows-input-pause-v31-r2.zip'
    with zipfile.ZipFile(archive) as z:
        z.extractall(client)
    manifest = json.loads((client / 'manifest.json').read_text())
    assert len(manifest) == 9
    for name, digest in manifest.items():
        assert hashlib.sha256((client / name).read_bytes()).hexdigest() == digest
    template = (f1 / 'windows_client/update_input_pause.ps1').read_text()
    probe = template.split("$probe=@'\n")[1].split("\n'@")[0]
    result = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(client)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'sender import check OK' in result.stdout
    assert launcher.read_text() == 'existing launcher'
    script = (output / 'update_input_pause_r2.ps1').read_text(encoding='utf-8-sig')
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in script
    assert '__ZIP_SHA256__' not in script
    # A missing dependency is caught by the exact installer preflight probe.
    (client / 'src/dagger_core/f710_mapping.py').unlink()
    result = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(client)],
                            capture_output=True, text=True)
    assert result.returncode != 0
