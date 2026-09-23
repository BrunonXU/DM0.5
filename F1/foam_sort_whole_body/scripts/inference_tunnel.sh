#!/usr/bin/env bash
# A separate, bounded forward over the existing F1 login tunnel; no robot commands.
set -euo pipefail
socket_dir="/tmp/f1-inference-tunnel-$UID"
if [[ -L "$socket_dir" || ( -e "$socket_dir" && ! -O "$socket_dir" ) ]]; then
  echo "SSH控制目录不是当前用户的普通目录：$socket_dir" >&2
  exit 2
fi
mkdir -p -m 700 "$socket_dir"
socket_path="$socket_dir/master.sock"
ssh_args=(-F /dev/null -i /home/xza_100850113/.ssh/id_ed25519_f1
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o HostKeyAlias=f1-d070 -o ExitOnForwardFailure=yes -o ConnectTimeout=10
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -p 22022
  -S "$socket_path")
case "${1:-status}" in
  start)
    if ssh "${ssh_args[@]}" -O check user@127.0.0.1 2>/dev/null; then
      echo 'F1推理转发已运行：5K 7892；20K 7894'
      exit 0
    fi
    ssh "${ssh_args[@]}" -M -fNT \
      -R 127.0.0.1:7892:127.0.0.1:7892 \
      -R 127.0.0.1:7894:127.0.0.1:7894 user@127.0.0.1
    ssh "${ssh_args[@]}" -O check user@127.0.0.1
    echo 'F1推理转发已建立：5K 7892；20K 7894'
    ;;
  status) ssh "${ssh_args[@]}" -O check user@127.0.0.1 ;;
  stop) ssh "${ssh_args[@]}" -O exit user@127.0.0.1 ;;
  *) echo '用法：inference_tunnel.sh start|status|stop（6000D）' >&2; exit 2 ;;
esac
