#!/usr/bin/env python3
"""使用磁盘样本检查DM0.5 HTTP服务的离线shadow客户端。

用途：不连接GDK，用三张本地图像和一个state JSON验证推理服务。
输入：头、左腕、右腕图片，34D state，固定prompt和HTTP endpoint。
输出：严格50×34动作、请求延迟及可选JSON结果文件；绝不发送真机动作。
方法：图片转RGB后复用``DM05HTTPBackend``，避免维护第二套HTTP协议。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import numpy as np

from g2_sort.config.dm05_contract import ACTION_DIM, PROMPT
from g2_sort.deployment.g2_client.contracts import Observation
from g2_sort.deployment.g2_client.dm05_backend import DM05HTTPBackend


def _read_rgb(path: Path) -> np.ndarray:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to read shadow images") from exc
    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
    except Exception as exc:
        raise ValueError(f"could not decode image {path}: {exc}") from exc


def request_action(
    endpoint: str,
    images: list[Path],
    state: list[float],
    timeout: float,
) -> tuple[list[list[float]], float]:
    if len(images) != 3:
        raise ValueError(f"expected three images, got {len(images)}")
    if len(state) != ACTION_DIM:
        raise ValueError(f"expected {ACTION_DIM}D state, got {len(state)}")

    observation = Observation(
        request_id=0,
        captured_at=time.monotonic(),
        images=tuple(_read_rgb(path) for path in images),
        state=np.asarray(state, dtype=np.float32),
        prompt=PROMPT,
    )
    backend = DM05HTTPBackend(endpoint, timeout)
    try:
        chunk = backend.infer(observation)
    finally:
        backend.close()
    return chunk.actions.tolist(), chunk.latency_seconds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Request actions and record them; never commands robot hardware."
    )
    parser.add_argument(
        "--endpoint",
        default="http://10.130.153.114:7892/process_frame",
    )
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--left-wrist", type=Path, required=True)
    parser.add_argument("--right-wrist", type=Path, required=True)
    parser.add_argument("--state-json", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    state_payload = json.loads(args.state_json.read_text())
    state = state_payload["state"] if isinstance(state_payload, dict) else state_payload
    records = []
    for request_index in range(args.repeat):
        actions, latency = request_action(
            args.endpoint,
            [args.head, args.left_wrist, args.right_wrist],
            state,
            args.timeout,
        )
        records.append(
            {
                "request_index": request_index,
                "latency_seconds": latency,
                "actions": actions,
            }
        )
        print(
            f"request={request_index} latency={latency:.3f}s "
            f"shape={len(actions)}x{len(actions[0])}"
        )

    latencies = sorted(record["latency_seconds"] for record in records)
    p95_index = max(0, min(len(latencies) - 1, int(0.95 * len(latencies)) - 1))
    result = {
        "mode": "shadow_only",
        "endpoint": args.endpoint,
        "prompt": PROMPT,
        "latency_mean_seconds": statistics.fmean(latencies),
        "latency_p95_seconds": latencies[p95_index],
        "records": records,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
