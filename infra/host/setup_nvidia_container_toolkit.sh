#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

# Ubuntu's GUI updater can hold the APT locks through packagekitd. Stop it
# cleanly; never remove lock files, because that can corrupt package state.
if systemctl is-active --quiet packagekit.service; then
  systemctl stop packagekit.service
fi

apt_locks=(
  /var/lib/dpkg/lock-frontend
  /var/lib/dpkg/lock
  /var/lib/apt/lists/lock
  /var/cache/apt/archives/lock
)
for attempt in $(seq 1 60); do
  lock_busy=false
  for apt_lock in "${apt_locks[@]}"; do
    if fuser "${apt_lock}" >/dev/null 2>&1; then
      lock_busy=true
      break
    fi
  done
  if [[ "${lock_busy}" == false ]]; then
    break
  fi
  if [[ ${attempt} -eq 60 ]]; then
    echo "APT lock is still busy after 120 seconds; aborting safely." >&2
    exit 2
  fi
  sleep 2
done

install -m 0755 -d /usr/share/keyrings
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor --yes \
  -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL \
  https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed \
    's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y \
  nvidia-container-toolkit=1.19.1-1 \
  nvidia-container-toolkit-base=1.19.1-1 \
  libnvidia-container-tools=1.19.1-1 \
  libnvidia-container1=1.19.1-1
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

echo "NVIDIA Container Toolkit installed and Docker restarted."
