#!/usr/bin/env bash
# Independent Full SFT 250 RTC service; never starts robot motion.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
projects=$(cd -- "$task_root/../.." && pwd -P)
name=f1-toys-full-250-rtc
release="$task_root/artifacts/20261020-v1-full-training/service-250"
bundle="$task_root/artifacts/20261020-v1-full-training/bundle-250"
frozen="$task_root/artifacts/h20-staging/f1-toys-20261020-v1-full-r1"
if docker inspect "$name" >/dev/null 2>&1; then
    docker start "$name"
    exit 0
fi
python3 - "$task_root" "$release" "$bundle" "$frozen" <<'PY'
import hashlib,json,shutil,sys
from pathlib import Path
task,out,bundle,frozen=map(Path,sys.argv[1:])
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()
manifest=json.loads((bundle/'bundle.json').read_text())
assert manifest['training_mode']=='full'
assert '/global_step_250/' in manifest['checkpoint']
for name,digest in manifest['files_sha256'].items():
    assert sha(bundle/name)==digest,name
if not out.exists():
    out.mkdir(parents=True)
    shutil.copytree(frozen/'tasks/F1_ComplexyToys/src/f1_toys',out/'src/f1_toys',
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    files={str(p.relative_to(out)):sha(p) for p in (out/'src').rglob('*') if p.is_file()}
    (out/'source-manifest.json').write_text(json.dumps(files,indent=2))
for name,digest in json.loads((out/'source-manifest.json').read_text()).items():
    assert sha(out/name)==digest,name
for name,digest in manifest['preprocessing_sha256'].items():
    assert sha(out/'src/f1_toys'/name)==digest,name
for name,digest in manifest['core_sha256'].items():
    assert sha(frozen/'RLinf/rlinf/models/embodiment/openpi_rlinf/pi0_model'/name)==digest,name
for name in ('logs','cache'):(out/name).mkdir(exist_ok=True)
print('Full 250 bundle, preprocessing and frozen core verified',flush=True)
PY
container_release="/workspace/${release#"$projects/"}/src"
docker run -d --name "$name" --restart unless-stopped --network host \
    --gpus device=2 --user "$(id -u):$(id -g)" \
    -e USER=user -e PYTHONDONTWRITEBYTECODE=1 -e OMP_NUM_THREADS=4 \
    -e MKL_NUM_THREADS=4 -e OPENBLAS_NUM_THREADS=4 -e TORCH_COMPILE_DISABLE=1 \
    -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor -e TRITON_CACHE_DIR=/cache/triton \
    -e NO_ALBUMENTATIONS_UPDATE=1 -e PYTHONPATH="$container_release" \
    -v "$projects:/workspace:ro" -v "$release/logs:/logs" -v "$release/cache:/cache" \
    --entrypoint /opt/conda/envs/opendm/bin/python \
    sha256:REDACTED_HOST_FINGERPRINT \
    -m f1_toys.server --rtc --rtc-compile-suffix --steps 5 \
    --host 127.0.0.1 --port 8150 \
    --bundle "/workspace/${bundle#"$projects/"}" \
    --rlinf-root "/workspace/${frozen#"$projects/"}/RLinf" \
    --dataset /workspace/tasks/F1_ComplexyToys/artifacts/20261020-home-time-mask/dataset/train \
    --log /logs/requests.jsonl
