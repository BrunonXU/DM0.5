#!/usr/bin/env bash
# Dedicated 5K RTC service. Existing sync containers and bindings are untouched.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
projects=$(cd -- "$task_root/../.." && pwd -P)
name=f1-toys-rtc-5000-v3
release="$task_root/artifacts/20261019-rtc/service-v3"
if docker inspect "$name" >/dev/null 2>&1; then
    docker start "$name"
    exit 0
fi
python3 - "$task_root" "$release" <<'PY'
import hashlib,json,shutil,sys
from pathlib import Path
task,out=map(Path,sys.argv[1:])
if not out.exists():
    out.mkdir(parents=True)
    shutil.copytree(task/'src/f1_toys',out/'src/f1_toys',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    files={str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (out/'src').rglob('*') if p.is_file()}
    (out/'source-manifest.json').write_text(json.dumps(files,indent=2))
for name,digest in json.loads((out/'source-manifest.json').read_text()).items():
    assert hashlib.sha256((out/name).read_bytes()).hexdigest()==digest
(out/'logs').mkdir(exist_ok=True)
(out/'cache').mkdir(exist_ok=True)
PY
source_container="/workspace/${release#"$projects/"}/src"
docker run -d --name "$name" --restart unless-stopped --network host \
    --gpus device=2 --user "$(id -u):$(id -g)" \
    -e USER=user -e PYTHONDONTWRITEBYTECODE=1 -e OMP_NUM_THREADS=4 \
    -e MKL_NUM_THREADS=4 -e OPENBLAS_NUM_THREADS=4 -e TORCH_COMPILE_DISABLE=1 \
    -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor -e TRITON_CACHE_DIR=/cache/triton -e NO_ALBUMENTATIONS_UPDATE=1 \
    -e PYTHONPATH="$source_container" -v "$projects:/workspace:ro" -v "$release/logs:/logs" -v "$release/cache:/cache" \
    --entrypoint /opt/conda/envs/opendm/bin/python \
    sha256:REDACTED_HOST_FINGERPRINT \
    -m f1_toys.server --rtc --rtc-compile-suffix --steps 5 --host 127.0.0.1 --port 8135 \
    --bundle /workspace/tasks/F1_ComplexyToys/artifacts/20261016-v1-formal-training/bundle-5000 \
    --rlinf-root /workspace/tasks/F1_ComplexyToys/artifacts/h20-staging/f1-toys-20261016-v1-formal-r3/RLinf \
    --dataset /workspace/tasks/F1_ComplexyToys/artifacts/20261016-v1-formal-training/dataset/train \
    --log /logs/requests.jsonl
