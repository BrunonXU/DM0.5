#!/usr/bin/env bash
set -Eeuo pipefail

NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
MOUNT_POINT="/mnt/dm05_nvme"
FSTAB_FILE="/etc/fstab"
FSTAB_ENTRY="UUID=${NVME_UUID} ${MOUNT_POINT} ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo $0" >&2
  exit 2
fi

DEVICE="$(blkid -U "${NVME_UUID}")"
if [[ -z "${DEVICE}" || ! -b "${DEVICE}" ]]; then
  echo "Cannot find block device with UUID=${NVME_UUID}." >&2
  exit 2
fi

install -d -m 0755 "${MOUNT_POINT}"

if mountpoint -q "${MOUNT_POINT}"; then
  mounted_uuid="$(findmnt -rn -T "${MOUNT_POINT}" -o UUID)"
  if [[ "${mounted_uuid}" != "${NVME_UUID}" ]]; then
    echo "Refusing to continue: ${MOUNT_POINT} contains UUID=${mounted_uuid:-unknown}." >&2
    exit 2
  fi
else
  # A desktop session may already expose the same filesystem under /media/<user>.
  # Mounting it directly here removes that username from all service paths.
  mount -U "${NVME_UUID}" "${MOUNT_POINT}"
fi

if ! grep -Eq "^[[:space:]]*UUID=${NVME_UUID}[[:space:]]+${MOUNT_POINT}[[:space:]]" "${FSTAB_FILE}"; then
  backup="${FSTAB_FILE}.dm05-backup-$(date +%Y%m%d-%H%M%S)"
  cp -a "${FSTAB_FILE}" "${backup}"
  printf '\n# DM0.5 shared NVMe data disk\n%s\n' "${FSTAB_ENTRY}" >>"${FSTAB_FILE}"
  echo "Updated ${FSTAB_FILE}; backup: ${backup}"
else
  echo "Matching ${FSTAB_FILE} entry already exists."
fi

systemctl daemon-reload
mount -a

mounted_uuid="$(findmnt -rn -T "${MOUNT_POINT}" -o UUID)"
if [[ "${mounted_uuid}" != "${NVME_UUID}" ]]; then
  echo "Post-install verification failed at ${MOUNT_POINT}." >&2
  exit 2
fi

echo
findmnt -T "${MOUNT_POINT}" -o SOURCE,TARGET,FSTYPE,UUID,OPTIONS
df -hT "${MOUNT_POINT}"

if [[ -d "${MOUNT_POINT}/dm05" ]]; then
  echo "PASS: ${MOUNT_POINT}/dm05 is accessible."
else
  echo "WARNING: mounted disk does not contain ${MOUNT_POINT}/dm05." >&2
  exit 2
fi
