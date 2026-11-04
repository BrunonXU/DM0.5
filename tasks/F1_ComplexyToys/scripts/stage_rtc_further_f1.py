"""Create an isolated F1 pixel-optimization release; does not activate or execute it."""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    staging = Path(__file__).parent
    base = Path('/home/user/Bruno/DAgger/releases/20261019-f1-toys-rtc-v4/DAgger')
    target = base.parents[1] / '20261020-f1-toys-rtc-v5-fastpixels/DAgger'
    runtime = Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/h20-rtc')
    with open('/home/user/Bruno/F1/runtime/control.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((base/'F1_RELEASE_MANIFEST.json').read_text())
        assert sha(base/'F1_RELEASE_MANIFEST.json') == 'e98cd68a08a4559bc42f9043cfa39c2a70619c9ab29eff6d916f34d3e4651820'
        for name, digest in manifest['files'].items():
            assert sha(base/name) == digest, name
        shutil.copytree(base, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
        changes = {}
        for name in ('rtc_pixels.py', 'rtc_policy.py'):
            relative = 'src/f1dagger/'+name
            shutil.copy2(staging/name, target/relative)
            changes[relative] = dict(before=manifest['files'][relative], after=sha(target/relative))
            manifest['files'][relative] = sha(target/relative)
        manifest['deployment_verified'] = False
        (target/'F1_RELEASE_MANIFEST.json').write_text(json.dumps(manifest, indent=2))
        manifest_sha = sha(target/'F1_RELEASE_MANIFEST.json')
        candidate = runtime/'fast-candidate'
        candidate.mkdir(exist_ok=False)
        for step in ('5000', '50000'):
            config = json.loads((runtime/f'config-{step}.json').read_text())
            config['accepted_release_manifest_sha256'] = manifest_sha
            config['policy']['rtc_pixel_workers'] = 3
            (candidate/f'config-{step}.json').write_text(json.dumps(config, indent=2))
        entry = Path('/home/user/Bruno/DAgger/start_f1_toys_rtc_h20.sh').read_text()
        assert entry.count(str(base)) == 1
        entry = entry.replace(str(base), str(target)).replace('h20-rtc/${checkpoint}/runtime',
            'h20-rtc/fast-candidate/${checkpoint}/runtime').replace(
            'h20-rtc/config-${checkpoint}.json', 'h20-rtc/fast-candidate/config-${checkpoint}.json')
        (candidate/'start.sh').write_text(entry)
        receipt = dict(base=str(base), release=str(target), manifest_sha256=manifest_sha,
                       changes=changes, candidate_entry=str(candidate/'start.sh'),
                       default_entry_changed=False, robot_commands=0)
        (candidate/'staging.json').write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
