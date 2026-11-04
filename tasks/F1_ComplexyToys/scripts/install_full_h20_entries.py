"""Install isolated F1 Full 625/1000 entries using verified v5; no robot IO."""
import fcntl
import hashlib
import json
from pathlib import Path


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_new(path, content):
    if path.exists():
        assert path.read_bytes()==content, f'Refuse overwrite: {path}'
    else:
        with path.open('xb') as f:f.write(content)


def main():
    base=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016')
    root=base/'full-h20'
    profiles=json.loads((root/'profiles.json').read_text())
    assert set(profiles)=={'625','1000'}
    config_template=json.loads((base/'h20-rtc/fast-candidate/config-5000.json').read_text())
    release=Path('/home/user/Bruno/DAgger/releases/20261020-f1-toys-rtc-v5-fastpixels/DAgger')
    with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest=release/'F1_RELEASE_MANIFEST.json'
        assert sha(manifest)==config_template['accepted_release_manifest_sha256']
        for name,digest in json.loads(manifest.read_text())['files'].items():
            assert sha(release/name)==digest,name
        entry_source=(root/'start_f1_full_h20.sh').read_bytes()
        assert b'--rtc-delay-steps 14 --rtc-min-steps 16' in entry_source
        prompt=json.loads((root/'contract.json').read_text())['prompt']
        assert prompt.encode() in entry_source
        configs={}
        for step,profile in profiles.items():
            dest=root/step;dest.mkdir(exist_ok=True)
            for name in ('contract.json','processing.py'):
                write_new(dest/name,(root/name).read_bytes())
            assert sha(dest/'contract.json')==profile['input_preprocessing']['contract_sha256']
            assert sha(dest/'processing.py')==profile['input_preprocessing']['implementation_sha256']
            config=json.loads(json.dumps(config_template))
            config['policy'].update(endpoint=f'http://127.0.0.1:{profile["local_port"]}',
                model_id=profile['model_id'],rtc_pixels_directory=str(dest),rtc_pixel_workers=3)
            write_new(dest/'config.json',(json.dumps(config,indent=2)+'\n').encode())
            configs[step]=dict(config_sha256=sha(dest/'config.json'),**profile)
        entry=Path('/home/user/Bruno/DAgger/start_f1_toys_full_h20.sh')
        write_new(entry,entry_source);entry.chmod(0o755)
        receipt=dict(entry=str(entry),entry_sha256=sha(entry),profiles=configs,
                     release_sha256=sha(manifest),rtc_delay_steps=14,rtc_min_steps=16,
                     robot_commands=0,existing_entries_unchanged=True)
        write_new(root/'installation.json',(json.dumps(receipt,indent=2)+'\n').encode())
        print(json.dumps(receipt))


if __name__=='__main__':main()
