"""Overlay only reviewed recovery deltas on both installed F1 frozen clients.

The manual schema3 and policy schema4 clients retain their independent hardware
contracts. This prepares immutable candidates; it never contacts the robot.
"""
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK.parents[1]
OUT = TASK / 'artifacts/20261023-manual-recovery'
STAGE = OUT / 'staging'
CLIENTS = {
    'manual': '/home/user/Bruno/DAgger/releases/20261023-f1-manual-recovery-r1/DAgger',
    'hil': '/home/user/Bruno/DAgger/releases/20261023-f1-hil-recovery-r1/DAgger',
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    STAGE.mkdir(exist_ok=False)
    before = {name: OUT / 'core-before' / name for name in
              ('policy.py', 'runtime.py', 'episode_controls.py', 'robot_io.py')}
    before['rtc_hil_feedback.py'] = ROOT / 'F1-4242/results/hil-recovery-20261023/before/dagger-rtc_hil_feedback.py'
    changes = {}
    for name, original in before.items():
        current = ROOT / 'DAgger/F1/src/f1dagger' / name
        relative = 'src/f1dagger/' + name
        delta = ''.join(difflib.unified_diff(original.read_text().splitlines(True),
                       current.read_text().splitlines(True), fromfile='a/' + relative,
                       tofile='b/' + relative))
        assert delta, name
        changes[relative] = dict(before=sha(original), after=sha(current), delta=delta)
    dump(STAGE / 'source-deltas.json', changes)
    packages = {}
    for mode, destination in CLIENTS.items():
        source = OUT / 'baseline' / mode / 'DAgger'
        release = STAGE / mode / 'DAgger'
        manifest = json.loads((source / 'F1_RELEASE_MANIFEST.json').read_text())
        for relative, expected in manifest['files'].items():
            assert sha(source / relative) == expected, relative
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        before_files = manifest['files'].copy()
        for relative, change in changes.items():
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1'], input=change['delta'],
                           cwd=release, text=True, check=True, capture_output=True)
            manifest['files'][relative] = sha(release / relative)
        manifest['deployment_verified'] = False
        manifest['manual_recovery'] = 'same_episode_release_neutral_rb_v1'
        dump(release / 'F1_RELEASE_MANIFEST.json', manifest)
        package = STAGE / (mode + '.tar.gz')
        with tarfile.open(package, 'w:gz') as tar:
            for name in [*manifest['files'], 'F1_RELEASE_MANIFEST.json']:
                assert not (release / name).is_symlink()
                tar.add(release / name, arcname='DAgger/' + name, recursive=False)
        packages[mode] = dict(destination=destination, archive=str(package),
            archive_sha256=sha(package), manifest_sha256=sha(release / 'F1_RELEASE_MANIFEST.json'),
            changed={n: dict(before=before_files[n], after=manifest['files'][n]) for n in changes},
            unchanged_count=sum(before_files[n] == manifest['files'][n] for n in before_files))
    config = json.loads((OUT / 'baseline/installed/manual-config.json').read_text())
    config['accepted_release_manifest_sha256'] = packages['manual']['manifest_sha256']
    dump(STAGE / 'manual-config.json', config)
    entry = (OUT / 'baseline/installed/manual-entry.sh').read_text()
    old_release = '/home/user/Bruno/DAgger/releases/20261021-f1-hil-v33-continuous-episodes-shadow-off-20261022/DAgger'
    old_config = '/home/user/Bruno/DAgger-runtime/continuous-episodes-20261021/manual/config.json'
    assert entry.count(old_release) == entry.count(old_config) == 1
    entry = entry.replace(old_release, CLIENTS['manual']).replace(old_config,
        '/home/user/Bruno/DAgger-runtime/manual-recovery-20261023/config.json')
    (STAGE / 'manual-entry.sh').write_text(entry)
    catalog_path = TASK / 'configs/inference_runtime_f1_09.json'
    catalog = json.loads(catalog_path.read_text())
    installed = json.loads((OUT / 'baseline/inference-current/catalog.json').read_text())
    assert catalog == installed, 'Inference catalog changed: inspect before composing'
    catalog['release'] = 'unified-ab-full-recovery-20261023-r2'
    catalog['robot_release'] = '/home/user/Bruno/DAgger-runtime/' + catalog['release']
    catalog['modes']['hil']['client'] = CLIENTS['hil']
    catalog['modes']['hil']['manifest_sha256'] = packages['hil']['manifest_sha256']
    dump(STAGE / 'catalog.json', catalog)
    subprocess.run([sys.executable, str(TASK / 'scripts/inference_runtime.py'),
        '--catalog', str(STAGE / 'catalog.json'), '--build', str(STAGE / 'assembly')], check=True)
    # The common console changes only the HIL recovery instruction. Rollout,
    # math, server entries and all rollout configurations stay byte-identical.
    assembly_changes = []
    for old in (OUT / 'baseline/inference-current').rglob('*'):
        if not old.is_file():
            continue
        relative = old.relative_to(OUT / 'baseline/inference-current')
        if sha(old) != sha(STAGE / 'assembly' / relative):
            assembly_changes.append(str(relative))
    assert set(assembly_changes) == {'catalog.json', 'assembly-manifest.json', 'inference_console.py',
                                   'configs/v1_dagger_rightonly_1000-hil.json'}, assembly_changes
    dump(STAGE / 'summary.json', dict(packages=packages, assembly_changed=assembly_changes,
        windows_update_required=False, hardware_contracts_preserved=True))
    print(json.dumps(dict(packages=packages, assembly_changed=assembly_changes), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
