#!/usr/bin/env bash
set -euo pipefail

# Download the exact DM0.5 base snapshot used by this project. Wget -c makes
# interrupted large-file transfers resumable without installing another CLI.

dm05_revision="581279826a339f0d90de15f145ec1a96aeea081e"
dm05_repo="Dexmal/DM05"
dm05_project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dm05_output_dir="${1:-${dm05_project_root}/server/model_cache/Dexmal/DM05}"
dm05_base_url="https://huggingface.co/${dm05_repo}/resolve/${dm05_revision}"

dm05_files=(
    ".gitattributes"
    "README.md"
    "chat_template.jinja"
    "config.json"
    "generation_config.json"
    "model.safetensors"
    "norm_stats.json"
    "preprocessor_config.json"
    "processor_config.json"
    "replay.mp4"
    "tokenizer.json"
    "tokenizer_config.json"
)

dm05_sizes=(
    "1617"
    "6826"
    "1532"
    "6795"
    "204"
    "11658431136"
    "3483"
    "419"
    "560"
    "86844448"
    "33384567"
    "794"
)

mkdir -p "${dm05_output_dir}"

for dm05_index in "${!dm05_files[@]}"; do
    dm05_file="${dm05_files[${dm05_index}]}"
    dm05_expected_size="${dm05_sizes[${dm05_index}]}"
    dm05_target="${dm05_output_dir}/${dm05_file}"

    if [[ -f "${dm05_target}" ]] && \
       [[ "$(stat -c '%s' "${dm05_target}")" == "${dm05_expected_size}" ]]; then
        echo "Already complete: ${dm05_file}"
        continue
    fi

    wget \
        --continue \
        --retry-connrefused \
        --waitretry=5 \
        --timeout=30 \
        --tries=0 \
        --output-document="${dm05_target}" \
        "${dm05_base_url}/${dm05_file}?download=true"

    dm05_actual_size="$(stat -c '%s' "${dm05_target}")"
    if [[ "${dm05_actual_size}" != "${dm05_expected_size}" ]]; then
        echo "Size mismatch for ${dm05_file}: expected ${dm05_expected_size}, got ${dm05_actual_size}" >&2
        exit 1
    fi
done

(
    cd "${dm05_output_dir}"
    echo "b7da77f516ebd0c0c68faed2f0cfe1c68985b7b874e49dda4292d8d286de20dd  model.safetensors" | sha256sum --check -
    echo "7d7ac6b1a6bec177ed4fa783b25a6f92dde5ec0defcbbfc27ecd5870a6a9afaf  replay.mp4" | sha256sum --check -
    echo "daab2354f8a74e70d70b4d1f804939b68a8c9624dd06cb7858e52dd8970e9726  tokenizer.json" | sha256sum --check -
)

echo "DM0.5 snapshot ${dm05_revision} downloaded and verified at ${dm05_output_dir}"
