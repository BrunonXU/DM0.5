#!/usr/bin/env bash
set -euo pipefail
connect_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
bash "$connect_dir/inference_forward.sh"
ssh -F "$connect_dir/client_ssh_config" -S "$connect_dir/inference-forward.sock" -O forward -R 127.0.0.1:7899:127.0.0.1:7899 f1-22029 2>/dev/null || true
# A duplicate forward is harmless only if the actual endpoint verifies below.
ssh -F "$connect_dir/client_ssh_config" f1-22029 'python3 -' <<'PY'
import urllib.request,json
for key,step in [('5k',5000),('20k',20000),('25k',25000)]:
 with urllib.request.urlopen(f'http://127.0.0.1:7899/{key}/health',timeout=10) as r:h=json.load(r)
 checkpoint=h['model_binding']['checkpoint_identity']['checkpoint']
 assert h['ready'] and h['execute_steps']==20 and checkpoint.endswith('/checkpoint-'+str(step))
 assert h['ik_recipe_version']=='f1_ikfk_demo_pos5mm_v1'
 assert h['ik_options']=={'position_tolerance_mm':5.0,'rotation_tolerance_deg':0.001}
 print(key,'OOD position tolerance 5mm ready:',checkpoint)
PY
