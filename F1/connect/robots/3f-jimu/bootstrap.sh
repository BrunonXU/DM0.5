#!/usr/bin/env bash
# Run once in the user terminal on 3F-JIMU, not on 6000D.
set -euo pipefail
if [[ $(id -un) != user || $HOME != /home/user ]]; then
    echo 'Expected F1 account user with HOME=/home/user; stop and confirm account.' >&2
    exit 1
fi
umask 077
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
if ! grep -qF 'AAAAC3NzaC1lZDI1NTE5AAAAIKrEFBJHd+8s1BqH6RwOvRd5TYk4qoe5YsHcPQylodAQ' "$HOME/.ssh/authorized_keys"; then
    cp -p "$HOME/.ssh/authorized_keys" "$HOME/.ssh/authorized_keys.before_3f_jimu_$(date -u +%Y%m%dT%H%M%SZ)"
    printf '\n%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKrEFBJHd+8s1BqH6RwOvRd5TYk4qoe5YsHcPQylodAQ 6000D-to-F1-D070' >> "$HOME/.ssh/authorized_keys"
fi
chmod 600 "$HOME/.ssh/authorized_keys"
mkdir -p /home/user/Bruno/connect/evidence
bootstrap_hosts=$(mktemp /home/user/Bruno/connect/evidence/bootstrap_known_hosts.XXXXXX)
trap 'rm -f "$bootstrap_hosts"' EXIT
printf '%s\n' '10.130.153.114 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM503bgnnyQLpFMpu/aZ69OFHx+3KsBuigjxoBu5nC41' > "$bootstrap_hosts"
hostname
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
echo 'Enter the 6000D account password if prompted. Keep this terminal open after connection.'
ssh -F /dev/null -NT -o PubkeyAuthentication=no \
    -o UserKnownHostsFile="$bootstrap_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 \
    -o ConnectTimeout=10 -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -R 127.0.0.1:22165:127.0.0.1:22 xza_100850113@10.130.153.114
