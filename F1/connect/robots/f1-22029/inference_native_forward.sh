#!/usr/bin/env bash
set -euo pipefail
connect_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
bash "$connect_dir/inference_forward.sh"
ssh -F "$connect_dir/client_ssh_config" -S "$connect_dir/inference-forward.sock" -O forward -R 127.0.0.1:7898:127.0.0.1:7898 f1-22029 2>/dev/null || true
ssh -F "$connect_dir/client_ssh_config" f1-22029 'python3 -' <<'PY'
import urllib.request,json
for key,step in [('5k',5000),('20k',20000),('25k',25000)]:
 with urllib.request.urlopen(f'http://127.0.0.1:7898/{key}/health',timeout=10) as r:h=json.load(r)
 checkpoint=h['model_binding']['checkpoint_identity']['checkpoint']
 assert h['ready'] and h['execute_steps']==50 and h['execution_mode']=='async_native_h50_v1' and checkpoint.endswith('/checkpoint-'+str(step))
 print(key,'native H50 ready:',checkpoint)
PY
