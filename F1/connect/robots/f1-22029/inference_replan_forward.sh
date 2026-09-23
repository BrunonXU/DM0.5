#!/usr/bin/env bash
# Run on 6000D after F1 is online. Network setup only; no robot control.
set -euo pipefail
connect_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
bash "$connect_dir/inference_forward.sh"
ssh -F "$connect_dir/client_ssh_config" -S "$connect_dir/inference-forward.sock" -O forward \
  -R 127.0.0.1:7902:127.0.0.1:7902 f1-22029 2>/dev/null || true
# Duplicate forwarding is acceptable only when every endpoint below verifies.
ssh -F "$connect_dir/client_ssh_config" f1-22029 'python3 -' <<'PY'
import urllib.request,json
for key,step in [('5k',5000),('20k',20000),('25k',25000),('70k',70000)]:
 with urllib.request.urlopen(f'http://127.0.0.1:7902/{key}/health',timeout=10) as response:
  health=json.load(response)
 checkpoint=health['model_binding']['checkpoint_identity']['checkpoint']
 assert health['ready'] and health['execute_steps']==20 and checkpoint.endswith('/checkpoint-'+str(step))
 assert health['ik_recipe_version']=='f1_ikfk_demo_pos5mm_rot005deg_v1'
 assert health['ik_options']=={'position_tolerance_mm':5.0,'rotation_tolerance_deg':0.05}
 assert health['prefix_policy']=='f1_ik_valid_prefix_replan_v1'
 print(key,'valid-prefix replan ready:',checkpoint)
PY
