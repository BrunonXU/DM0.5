"""Switch the reviewed F1 entry only while control is idle; no robot IO."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    base = Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20')
    candidate = base/'startup-read-v2'
    entry = Path('/home/user/Bruno/DAgger/start_f1_toys_full_h20.sh')
    source = candidate/'start_f1_full_h20_candidate.sh'
    receipt = json.loads((candidate/'preparation.json').read_text())
    old_sha = 'bd729b41693de2e6a6a6fa62435aef6cd1bef7d90595c8c26304dcb810774251'
    new_sha = '74e31663871219a117a96755234fa4b34f612de11f53e4df9a3c7b238e044c35'
    assert sha(source) == new_sha
    subprocess.run(['bash', '-n', str(source)], check=True)
    with open('/home/user/Bruno/F1/runtime/control.lock', 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'switched':False, 'reason':'control_busy', 'robot_commands':0}))
            return 3
        assert sha(entry) == old_sha, 'Current entry changed; refuse overwrite'
        for ck, binding in receipt['configs'].items():
            before = base/ck/'config.json'
            after = Path(binding['path'])
            assert sha(before) == binding['old_sha256'] and sha(after) == binding['new_sha256']
            old_config, new_config = json.loads(before.read_text()), json.loads(after.read_text())
            old_config['accepted_release_manifest_sha256'] = receipt['release_sha256']
            assert old_config == new_config, 'Unexpected policy/configuration change'
        release = Path(receipt['release'])
        assert sha(release/'F1_RELEASE_MANIFEST.json') == receipt['release_sha256']
        manifest = json.loads((release/'F1_RELEASE_MANIFEST.json').read_text())
        for name, digest in manifest['files'].items():
            assert sha(release/name) == digest, name
        infra = Path(receipt['infra'])
        deps = json.loads((release/'configs/f1_infra_binding.json').read_text())
        for name, digest in deps['files'].items():
            assert sha(infra/name) == digest, name
        backup = candidate/'start_f1_toys_full_h20.before.sh'
        with backup.open('xb') as f:
            f.write(entry.read_bytes())
        backup.chmod(entry.stat().st_mode & 0o777)
        temporary = entry.with_name(entry.name+'.param-read.tmp')
        with temporary.open('xb') as f:
            f.write(source.read_bytes())
        temporary.chmod(0o755)
        os.replace(temporary, entry)
        assert sha(entry) == new_sha
        result = dict(switched=True, observed_utc=datetime.now(timezone.utc).isoformat(),
                      entry=str(entry), old_sha256=old_sha, new_sha256=new_sha,
                      backup=str(backup), release_sha256=receipt['release_sha256'],
                      robot_commands=0, services_restarted=False,
                      unchanged=['model', 'D14/S16', 'IK', 'motion_limits', 'motor_driver', 'old_releases'])
        (candidate/'switch-receipt.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
