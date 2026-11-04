"""Stage a manifest-bound read-only parameter fix from the actual F1 v5 release.

Does not switch an entry, create command publishers, or touch the motor driver.
"""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    base = Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20')
    old = Path('/home/user/Bruno/DAgger/releases/20261020-f1-toys-rtc-v5-fastpixels/DAgger')
    old_infra = Path('/home/user/Bruno/F1/F1-4242/releases/20261010-f1-4242-v25-hil-routes/workspace/F1-infra')
    new = old.parents[1]/'20261021-f1-toys-rtc-v6-param-read-r2'/'DAgger'
    target = base/'startup-read-v2'
    candidate = base/'runtime_contract_candidate_20261021_v2.py'
    expected_candidate = 'ff36d5e70fc57f531425786d1879744e187e4a68bd0e627d3169b6bb6e61f672'
    assert sha(candidate) == expected_candidate
    # Staging writes only new, unselected directories. Serialize staging itself;
    # the later entry switch must separately hold the robot's control.lock.
    with (base/'parameter-read-staging.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not new.exists() and not target.exists(), 'Candidate destination exists; inspect before retry'
        manifest_path = old/'F1_RELEASE_MANIFEST.json'
        assert sha(manifest_path) == '7550ed5cc85c7c1c48569d917c8ab109a47119d7e5b1c7c91d0df8be8b69707a'
        release = json.loads(manifest_path.read_text())
        for name, digest in release['files'].items():
            assert sha(old/name) == digest, name
        binding = json.loads((old/'configs/f1_infra_binding.json').read_text())
        for name, digest in binding['files'].items():
            assert sha(old_infra/name) == digest, name
        assert binding['files']['robot/runtime_contract.py'] == '8f9db63b55e099afc61bddf421ee23674b34994531f65e9edc0394920c9ad4ca'
        required = sum((old_infra/name).stat().st_size for name in binding['files'])
        required += sum((old/name).stat().st_size for name in release['files'])
        assert shutil.disk_usage(base).free > required*2 + 100_000_000
        target.mkdir()
        infra = target/'infra'
        for name in binding['files']:
            dst = infra/name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_infra/name, dst)
        shutil.copy2(candidate, infra/'robot/runtime_contract.py')
        # Copy only manifest files, not caches or any mutable recording/runtime.
        new.mkdir(parents=True)
        for name in release['files']:
            dst = new/name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old/name, dst)
        binding['files']['robot/runtime_contract.py'] = expected_candidate
        (new/'configs/f1_infra_binding.json').write_text(json.dumps(binding, indent=2)+'\n')
        release['files']['configs/f1_infra_binding.json'] = sha(new/'configs/f1_infra_binding.json')
        (new/'F1_RELEASE_MANIFEST.json').write_text(json.dumps(release, indent=2)+'\n')
        configs = {}
        for ck in ('625', '1000'):
            cfg = json.loads((base/ck/'config.json').read_text())
            old_sha = sha(base/ck/'config.json')
            cfg['accepted_release_manifest_sha256'] = sha(new/'F1_RELEASE_MANIFEST.json')
            (target/ck).mkdir()
            path = target/ck/'config.json'
            path.write_text(json.dumps(cfg, indent=2)+'\n')
            configs[ck] = dict(old_sha256=old_sha, new_sha256=sha(path), path=str(path))
        for name, digest in binding['files'].items():
            assert sha(infra/name) == digest, name
        for name, digest in release['files'].items():
            assert sha(new/name) == digest, name
        assert sha(manifest_path) == '7550ed5cc85c7c1c48569d917c8ab109a47119d7e5b1c7c91d0df8be8b69707a'
        receipt = dict(release=str(new), infra=str(infra), configs=configs,
                       release_sha256=sha(new/'F1_RELEASE_MANIFEST.json'),
                       parameter_probe_sha256=expected_candidate,
                       changed_dependency_files=['robot/runtime_contract.py'],
                       changed_release_files=['configs/f1_infra_binding.json'],
                       entry_switched=False, robot_commands=0)
        (target/'preparation.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
