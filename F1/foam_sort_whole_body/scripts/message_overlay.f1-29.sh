#!/usr/bin/env bash
# Source AFTER the original ROS/f1/agv setup. These two older agv packages omit State.
if [[ "$(cat /etc/machine-id)" != 00000000000000000000000000000001 ]]; then
  echo '4F-F1消息配置与本机身份不匹配' >&2; return 2
fi
for f1_message_pkg in driver server; do
  f1_message_prefix="/home/user/lxl/interface_20260623/install/$f1_message_pkg"
  if [[ ! -f "$f1_message_prefix/share/$f1_message_pkg/msg/State.idl" ]]; then
    echo "缺少4F-F1已核对消息包：$f1_message_prefix" >&2; return 2
  fi
  export AMENT_PREFIX_PATH="$f1_message_prefix${AMENT_PREFIX_PATH:+:$AMENT_PREFIX_PATH}"
  export LD_LIBRARY_PATH="$f1_message_prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PYTHONPATH="$f1_message_prefix/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
done
unset f1_message_pkg f1_message_prefix
