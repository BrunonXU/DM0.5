"""Stage two isolated Full RTC services on H20; no training or robot calls."""
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    projects = Path('/home/user/projects')
    frozen = projects/'task-releases/f1-toys-20261020-v1-full-r1'
    target = projects/'task-releases/f1-toys-20261021-full-rtc'
    source = frozen/'tasks/F1_ComplexyToys/src/f1_toys'
    wrapper = Path(__file__).with_name('h20_rtc_fast_server.py')
    validated = projects/'task-releases/f1-toys-20261020-rtc-h20-v4-fastmath/h20_rtc_fast_server.py'
    assert sha(wrapper) == sha(validated), 'Reuse the validated runtime wrapper exactly'
    target.mkdir(exist_ok=False)
    shutil.copytree(source, target/'src/f1_toys', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(wrapper, target/wrapper.name)
    profiles = {}
    for step, gpu, port, local_port in ((625,0,8137,8152),(1000,1,8138,8153)):
        bundle = Path(f'/u01/user/runs/f1-toys-v1-full-20261020-r1/bundle-{step}')
        manifest = json.loads((bundle/'bundle.json').read_text())
        assert manifest['training_mode']=='full' and f'/global_step_{step}/' in manifest['checkpoint']
        for name,digest in manifest['files_sha256'].items():
            assert sha(bundle/name)==digest,name
        for name,digest in manifest['preprocessing_sha256'].items():
            assert sha(target/'src/f1_toys'/name)==digest,name
        for name,digest in manifest['core_sha256'].items():
            assert sha(frozen/'RLinf/rlinf/models/embodiment/openpi_rlinf/pi0_model'/name)==digest,name
        run = Path(f'/u01/user/runs/f1-toys-full-{step}-rtc-20261021')
        run.mkdir(exist_ok=False)
        script = f'''#!/usr/bin/env bash
set -euo pipefail
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export CUDA_VISIBLE_DEVICES={gpu}
export PYTHONPATH={target}/src
export TORCH_COMPILE_DISABLE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NO_ALBUMENTATIONS_UPDATE=1
export TORCHINDUCTOR_CACHE_DIR={run}/cache/inductor
export TRITON_CACHE_DIR={run}/cache/triton
mkdir -p {run}/logs
exec python -u {target}/h20_rtc_fast_server.py --rtc --rtc-compile-suffix --steps 5 --host 127.0.0.1 --port {port} \\
 --bundle {bundle} \\
 --rlinf-root {frozen}/RLinf \\
 --dataset /u01/user/datasets/f1_toys_v1_home_mask_20261020/dataset/train \\
 --log {run}/logs/requests.jsonl
'''
        (run/'start.sh').write_text(script)
        version=sha(bundle/'bundle.json')
        profiles[str(step)]=dict(local_port=local_port,remote_port=port,gpu=gpu,
            model_id='f1-toys-eef-'+version[:16],model_version=version,
            input_preprocessing=dict(implementation_sha256=manifest['preprocessing_sha256']['processing.py'],
                                     contract_sha256=manifest['files_sha256']['contract.json']),
            start=str(run/'start.sh'),run=str(run),bundle=str(bundle),
            start_sha256=sha(run/'start.sh'))
    (target/'SOURCE_MANIFEST.json').write_text(json.dumps({str(f.relative_to(target)):sha(f)
        for f in target.rglob('*') if f.is_file()},indent=2))
    (target/'profiles.json').write_text(json.dumps(profiles,indent=2))
    print(json.dumps(profiles),flush=True)


if __name__=='__main__':
    main()
