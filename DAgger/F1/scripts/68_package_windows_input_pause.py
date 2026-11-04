"""Build a self-contained v31 Windows sender update, never a partial core tree."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(output: Path):
    root = Path(__file__).resolve().parents[1] / 'windows_client'
    names = ['scripts/03_windows_f1_sender.py', 'scripts/03_windows_shadow_sender.py']
    names += ['src/dagger_core/' + n + '.py' for n in (
        '__init__', 'protocol', 'async_transport', 'input_recovery',
        'f710_mapping', 'gamepad_windows', 'sender_guidance')]
    payload = {name: (root / name).read_bytes() for name in names}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'windows-input-pause-v31-r2.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        for name, data in payload.items():
            z.writestr(name, data)
        z.writestr('manifest.json', json.dumps(manifest, indent=2))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    template = (root / 'update_input_pause.ps1').read_text()
    (output / 'update_input_pause_r2.ps1').write_text(
        template.replace('__ZIP_SHA256__', digest), encoding='utf-8-sig')
    (output / 'package.json').write_text(json.dumps({'sha256': digest, 'files': manifest}, indent=2))
    print(digest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
