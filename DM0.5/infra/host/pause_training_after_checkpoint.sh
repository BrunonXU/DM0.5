#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 4 ]]; then
  echo "usage: $0 STEP CHECKPOINT_ROOT TRAINING_TMUX STATUS_LOG" >&2
  exit 2
fi

step="$1"
checkpoint_root="$2"
training_tmux="$3"
status_log="$4"
checkpoint_dir="${checkpoint_root}/checkpoint-${step}"

mkdir -p "$(dirname "${status_log}")"
exec >>"${status_log}" 2>&1

printf 'watcher_start=%s target_step=%s checkpoint=%s tmux=%s\n' \
  "$(date --iso-8601=seconds)" "${step}" "${checkpoint_dir}" "${training_tmux}"

if ! tmux has-session -t "${training_tmux}" 2>/dev/null; then
  echo "training tmux does not exist: ${training_tmux}" >&2
  exit 1
fi

mapfile -t running_containers < <(sg docker -c "docker ps -q")
if [[ "${#running_containers[@]}" -ne 1 ]]; then
  echo "expected exactly one running training container, found ${#running_containers[@]}" >&2
  exit 1
fi
training_container="${running_containers[0]}"
container_command="$(
  sg docker -c "docker inspect --format '{{json .Config.Cmd}}' ${training_container}"
)"
if [[ "${container_command}" != *"dm05_g2_8gpu_50k_to_100k"* ]]; then
  echo "running container does not match the extension training contract" >&2
  exit 1
fi
printf 'training_container=%s\n' "${training_container}"

required_files=(
  adapter_model.safetensors
  optimizer.pt
  scheduler.pt
  trainer_state.json
  training_args.bin
  rng_state_0.pth
  rng_state_1.pth
  rng_state_2.pth
  rng_state_3.pth
  rng_state_4.pth
  rng_state_5.pth
  rng_state_6.pth
  rng_state_7.pth
)

while true; do
  ready=true
  for required_file in "${required_files[@]}"; do
    if [[ ! -s "${checkpoint_dir}/${required_file}" ]]; then
      ready=false
      break
    fi
  done
  if [[ "${ready}" == "true" ]] &&
     rg -q "\"global_step\": ${step}," "${checkpoint_dir}/trainer_state.json"; then
    break
  fi
  sleep 10
done

printf 'checkpoint_files_detected=%s\n' "$(date --iso-8601=seconds)"
snapshot_before="$(
  stat -c '%n:%s:%Y' \
    "${checkpoint_dir}/adapter_model.safetensors" \
    "${checkpoint_dir}/optimizer.pt" \
    "${checkpoint_dir}/scheduler.pt" \
    "${checkpoint_dir}/trainer_state.json" \
    "${checkpoint_dir}"/rng_state_*.pth
)"
sleep 20
snapshot_after="$(
  stat -c '%n:%s:%Y' \
    "${checkpoint_dir}/adapter_model.safetensors" \
    "${checkpoint_dir}/optimizer.pt" \
    "${checkpoint_dir}/scheduler.pt" \
    "${checkpoint_dir}/trainer_state.json" \
    "${checkpoint_dir}"/rng_state_*.pth
)"
if [[ "${snapshot_before}" != "${snapshot_after}" ]]; then
  echo "checkpoint files changed during stability window; refusing to interrupt" >&2
  exit 1
fi

sync -f "${checkpoint_dir}"
printf 'checkpoint_verified=%s action=send_ctrl_c\n' "$(date --iso-8601=seconds)"
tmux send-keys -t "${training_tmux}:0.0" C-c

for attempt in $(seq 1 90); do
  if ! sg docker -c \
    "docker inspect --format '{{.State.Running}}' ${training_container}" \
    2>/dev/null | rg -qx 'true'; then
    printf 'pause_complete=%s checkpoint_step=%s gpu_training_container_stopped=true\n' \
      "$(date --iso-8601=seconds)" "${step}"
    touch "$(dirname "${status_log}")/.paused_at_${step}"
    exit 0
  fi
  if [[ "${attempt}" -eq 30 ]]; then
    printf 'first_interrupt_timeout=%s action=send_second_ctrl_c\n' \
      "$(date --iso-8601=seconds)"
    tmux send-keys -t "${training_tmux}:0.0" C-c
  fi
  sleep 2
done

echo "checkpoint is safe, but the training container did not stop within 180 seconds" >&2
exit 1
