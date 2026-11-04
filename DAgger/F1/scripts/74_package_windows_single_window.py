"""Package the complete F1 operator client and atomically selected version; no robot actions."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(output, version):
    import re
    if not re.fullmatch(r'(\d{8}-r\d+|V\d+)', version):
        raise ValueError('Invalid package version')
    remote_directory = ('f1-ssh-session-'+version if version.startswith('V') else
                        version.replace('-r', '-single-window-r'))
    root = Path(__file__).resolve().parents[1]/'windows_client'
    names = ['start_f1_rlt.cmd', 'scripts/windows_f1_session.py', 'scripts/windows_session_logs.py',
             'scripts/windows_f1_bootstrap.py', 'scripts/03_windows_f1_sender.py', 'scripts/03_windows_shadow_sender.py']
    names += ['src/dagger_core/'+name+'.py' for name in (
        '__init__', 'protocol', 'async_transport', 'input_recovery', 'f710_mapping', 'gamepad_windows', 'sender_guidance')]
    payload = {name: (root/name).read_bytes() for name in names}
    # The installed script and its lifecycle log must identify the package that
    # actually contains them, never a previous hard-coded release number.
    marker = b"LAUNCHER_VERSION = 'development'"
    entry = 'scripts/windows_f1_session.py'
    if payload[entry].count(marker) != 1:
        raise ValueError('Launcher version placeholder missing or ambiguous')
    payload[entry] = payload[entry].replace(marker, ('LAUNCHER_VERSION = '+repr(version)).encode())
    payload['operator-package.json'] = json.dumps(dict(version=version, launcher=version, sender=version,
        operator_protocol='f1_rlt_local_v4', ack_contract='advancing_ack_v1',
        capabilities=['pending_review', 'bounded_same_session_resume', 'reviewed_expert_steps_v1']), indent=2).encode()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    output.mkdir(parents=True, exist_ok=True)
    archive = output/f'windows-single-window-{version}.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        for name, data in payload.items(): z.writestr(name, data)
        z.writestr('manifest.json', json.dumps(manifest, indent=2))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    template = (root/'update_operator_package.ps1').read_text()
    template = template.replace('__REMOTE__', remote_directory).replace('__VERSION__', version).replace('__ZIP_SHA256__', digest)
    (output/'install_single_window.ps1').write_text(template, encoding='utf-8-sig')
    (output/'package.json').write_text(json.dumps({'version': version, 'sha256': digest, 'files': manifest}, indent=2)+'\n')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--version', required=True, help='Previously allocated release version; never inferred from an old default')
    args=p.parse_args()
    print(json.dumps(build(args.output,args.version)))
