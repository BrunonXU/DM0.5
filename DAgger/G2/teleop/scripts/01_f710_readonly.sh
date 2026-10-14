#!/usr/bin/env bash
# F710 only-read probe. No GDK import, Robot construction, or command output.
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_dir="$(CDPATH= cd -- "${script_dir}/.." && pwd)"

export PYTHONPATH="${project_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${script_dir}/01_f710_readonly.py" "$@"
