#!/usr/bin/env python3
"""验证 G2 34D checkpoint 的完整交付合同。

检查checkpoint目录中的动作维度、模型配置、norm stats以及LoRA相关密集模块
是否齐全；只读文件并输出检查结果，不加载模型或连接机器人。本文件属于 G2
项目定制代码。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ACTION_DIM = 34


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text())


def validate(checkpoint: Path) -> list[str]:
    errors = []
    adapter_path = checkpoint / "adapter_config.json"
    config_path = checkpoint / "config.json"
    if config_path.exists():
        config = _read_json(config_path)
        if config.get("action_dim") != ACTION_DIM:
            errors.append(f"config.action_dim={config.get('action_dim')}, expected 34")
    elif not adapter_path.exists():
        errors.append("checkpoint has neither config.json nor adapter_config.json")

    norm_payload = _read_json(checkpoint / "norm_stats.json")
    stats = norm_payload.get("norm_stats", norm_payload)
    for key in ("state", "action"):
        for stat_name in ("mean", "std", "q01", "q99"):
            values = stats.get(key, {}).get(stat_name)
            if not isinstance(values, list) or len(values) != ACTION_DIM:
                errors.append(f"{key}.{stat_name} is not 34D")

    if adapter_path.exists():
        adapter = _read_json(adapter_path)
        modules = adapter.get("modules_to_save") or []
        for required in ("action_in_proj", "action_out_proj"):
            if not any(required in item for item in modules):
                errors.append(f"LoRA adapter does not save {required}")
        if not adapter.get("base_model_name_or_path"):
            errors.append("LoRA adapter does not identify its base model")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()
    errors = validate(args.checkpoint)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(2)
    print("checkpoint contract: PASS")


if __name__ == "__main__":
    main()
