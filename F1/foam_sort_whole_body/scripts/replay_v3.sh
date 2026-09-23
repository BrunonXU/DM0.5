#!/usr/bin/env bash
# Versioned F1 entry: default is read-only inspection, never implicit execution.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
client="$root/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body"
data="$root/runtime/replay_bundles/base_relabel_v3_20260915/bundles"
profile="$client/configs/replay_f1.json"
mode="${1:-inspect}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "$mode" == list ]]; then
  cat <<'EOF'
057  all_data_3_derived_episode_000057  28.000s  840 frames  arm clipping approved; live preflight required
003  all_data_2_derived_episode_000003  28.733s  862 frames  extra weak lateral-distance allocation
096  all_data_1_derived_episode_000096  35.067s 1052 frames
192  all_data_derived_episode_000192    52.967s 1589 frames
093  all_data_derived_episode_000093    55.667s 1670 frames
020  all_data_1_derived_episode_000020  34.167s 1025 frames
058  all_data_3_derived_episode_000058  25.800s  774 frames
135 excluded: 29.5-34.3s base labels remain unknown; no whole-task bundle.
These are numeric replay candidates. execute still checks the runtime profile and data status.
EOF
  exit 0
fi
case "$mode" in inspect|dry-run|check-env|feedback|preflight|execute) ;; *)
  echo 'Usage: ./replay_v3.sh [list|inspect|dry-run|check-env|feedback|preflight|execute] [057|003|096|192|093|020|058] [replay options]' >&2
  exit 2;;
esac
sample=057
if [[ $# -gt 0 && "$1" != --* ]]; then sample="$1"; shift; fi
case "$sample" in
  057|all_data_3_derived_episode_000057) episode=all_data_3_derived_episode_000057;;
  003|all_data_2_derived_episode_000003) episode=all_data_2_derived_episode_000003;;
  096|all_data_1_derived_episode_000096) episode=all_data_1_derived_episode_000096;;
  192|all_data_derived_episode_000192) episode=all_data_derived_episode_000192;;
  093|all_data_derived_episode_000093) episode=all_data_derived_episode_000093;;
  020|all_data_1_derived_episode_000020) episode=all_data_1_derived_episode_000020;;
  058|all_data_3_derived_episode_000058) episode=all_data_3_derived_episode_000058;;
  *) echo "Unknown or excluded source: $sample; use ./replay_v3.sh list" >&2; exit 2;;
esac
if [[ "$episode" == all_data_3_derived_episode_000057 ]]; then
  data="$root/runtime/replay_bundles/base_relabel_v3_clip_20260916/bundles"
fi
export PYTHONDONTWRITEBYTECODE=1
case "$mode" in
  check-env|feedback|preflight|execute)
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
    export ROS_LOG_DIR="$root/runtime/replay/ros_logs"
    mkdir -p "$ROS_LOG_DIR"
    source /home/user/workspace/agv/install/setup.bash
    ;;
esac
exec "$client/scripts/replay.sh" "$mode" --bundle "$data/$episode" --profile "$profile" "$@"
