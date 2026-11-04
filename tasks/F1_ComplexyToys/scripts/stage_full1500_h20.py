"""Register Full1500 using the deployed Full1000 runtime, without robot output."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    run = Path('/u01/user/runs/f1-toys-full-1500-rtc-20261021')
    runtime = Path('/home/user/projects/task-releases/f1-toys-20261021-full-rtc')
    core = Path('/home/user/projects/task-releases/f1-toys-20261020-v1-full-r1/RLinf')
    bundle = run / 'bundle'
    assert (run / 'export-exit-code.txt').read_text().strip() == '0'
    manifest = json.loads((bundle / 'bundle.json').read_text())
    assert manifest['training_mode'] == 'full' and '/global_step_1500/' in manifest['checkpoint']
    for name, digest in manifest['files_sha256'].items():
        assert sha(bundle / name) == digest, name
    for name, digest in manifest['preprocessing_sha256'].items():
        assert sha(runtime / 'src/f1_toys' / name) == digest, name
    for name, digest in manifest['core_sha256'].items():
        assert sha(core / 'rlinf/models/embodiment/openpi_rlinf/pi0_model' / name) == digest, name
    for name, digest in json.loads((runtime / 'SOURCE_MANIFEST.json').read_text()).items():
        assert sha(runtime / name) == digest, name
    # Check serialized tensors, rather than only trusting export metadata.
    import torch
    from safetensors import safe_open
    torch.set_num_threads(4)
    state = torch.load(manifest['checkpoint'], map_location='cpu', weights_only=True, mmap=True)
    with safe_open(str(bundle / 'model.safetensors'), framework='pt') as f:
        assert set(state) == {'model.' + k for k in f.keys()}
        for k in f.keys():
            assert torch.equal(state['model.' + k], f.get_tensor(k)), k
    del state
    old_run = run.with_name('f1-toys-full-1000-rtc-20261021')
    source = (old_run / 'start.sh').read_text()
    assert 'export CUDA_VISIBLE_DEVICES=1\n' in source and '--port 8138' in source
    script = source.replace('export CUDA_VISIBLE_DEVICES=1\n', 'export CUDA_VISIBLE_DEVICES=3\n')
    script = script.replace('--port 8138', '--port 8139').replace(str(old_run), str(run))
    script = script.replace('/u01/user/runs/f1-toys-v1-full-20261020-r1/bundle-1000', str(bundle))
    assert not (run / 'start.sh').exists()
    (run / 'start.sh').write_text(script)
    version = sha(bundle / 'bundle.json')
    profile = dict(local_port=8154, remote_port=8139, gpu=3,
        model_id='f1-toys-eef-' + version[:16], model_version=version,
        input_preprocessing=dict(implementation_sha256=manifest['preprocessing_sha256']['processing.py'],
                                 contract_sha256=manifest['files_sha256']['contract.json']),
        bundle=str(bundle), run=str(run), start=str(run / 'start.sh'), start_sha256=sha(run / 'start.sh'))
    (run / 'profile.json').write_text(json.dumps(profile, indent=2) + '\n')
    receipt = dict(profile=profile, full_export_tensor_exact=True,
        runtime_sha256=sha(runtime / 'SOURCE_MANIFEST.json'),
        ssh_permissions_changed=False, tunnel_permission_pending=True,
        robot_commands=0, existing_services_changed=False)
    (run / 'stage-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
