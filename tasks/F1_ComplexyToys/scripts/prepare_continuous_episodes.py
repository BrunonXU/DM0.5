"""Freeze minimal overlays onto the accepted policy/manual releases."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK.parents[1]
OUT = TASK/'artifacts/20261021-continuous-episodes'
PREVIOUS = TASK/'artifacts/20261021-rtc-hil'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    summaries = {}
    for mode, archive in [('policy', PREVIOUS/'client-isolated-r5.tar.gz'),
                          ('manual', OUT/'manual-v32-baseline.tar.gz')]:
        stage = OUT/(mode+'-staging')
        stage.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(stage, filter='data')
        release = stage/'DAgger'
        manifest = json.loads((release/'F1_RELEASE_MANIFEST.json').read_text())
        before = manifest['files'].copy()
        for name, digest in before.items():
            assert sha(release/name) == digest, name
        names = ['cli', 'policy', 'runtime', 'status', 'demonstration',
                 'episode_controls', 'rtc_hil_feedback']
        if mode == 'policy':
            names += ['rtc_hil']
        overlays = {f'src/f1dagger/{n}.py': ROOT/f'DAgger/F1/src/f1dagger/{n}.py' for n in names}
        for name in ['protocol', 'f710_mapping']:
            overlays[f'src/dagger_core/{name}.py'] = ROOT/f'DAgger/shared/src/dagger_core/{name}.py'
            overlays[f'windows_client/src/dagger_core/{name}.py'] = ROOT/f'DAgger/F1/windows_client/src/dagger_core/{name}.py'
        for name in ['03_windows_f1_sender', '03_windows_shadow_sender']:
            overlays[f'windows_client/scripts/{name}.py'] = ROOT/f'DAgger/F1/windows_client/scripts/{name}.py'
        if mode == 'manual':
            # Dependencies required by the already accepted shared CLI/policy;
            # preserve manual robot_io, IK, Home, config and infra bindings.
            baseline = PREVIOUS/'right-arm-r5-staging/DAgger'
            for name in ['src/f1dagger/standalone.py', 'src/f1dagger/policy_errors.py',
                         'src/dagger_core/powered_hil_policy.py',
                         'src/f1dagger/recording_audio.py',
                         'src/f1dagger/audio/takeover.wav', 'src/f1dagger/audio/resumed.wav']:
                overlays[name] = baseline/name
        for name, source in overlays.items():
            target = release/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest['files'][name] = sha(target)
        manifest['deployment_verified'] = False
        manifest['episode_controls'] = 'start_up_v2'
        (release/'F1_RELEASE_MANIFEST.json').write_text(json.dumps(manifest, indent=2)+'\n')
        changed = [n for n, d in before.items() if manifest['files'][n] != d]
        added = sorted(set(manifest['files'])-set(before))
        package = OUT/(mode+'-episodes.tar.gz')
        with tarfile.open(package, 'w:gz') as tar:
            # Manifest is the closure; never package runtime or caches.
            for name in [*manifest['files'], 'F1_RELEASE_MANIFEST.json']:
                tar.add(release/name, arcname='DAgger/'+name, recursive=False)
        summaries[mode] = dict(archive_sha256=sha(package), manifest_sha256=sha(release/'F1_RELEASE_MANIFEST.json'),
                              changed=changed, added=added, baseline_sha256=sha(archive))
    (OUT/'package-summary.json').write_text(json.dumps(summaries, indent=2)+'\n')
    print(json.dumps(summaries, indent=2))


if __name__ == '__main__':
    build()
