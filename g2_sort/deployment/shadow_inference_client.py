#!/usr/bin/env python3
"""DM0.5 G2 HTTP 推理服务的安全 shadow-mode 客户端。

输入三路图像、当前34D state和固定prompt，检查服务返回的action chunk形状、
有限性与延迟并写日志。本客户端不包含G2执行器调用，绝不会把模型输出下发
真机。本文件属于 G2项目定制代码。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import requests

from g2_sort.data_tools.g2_contract import ACTION_DIM, PROMPT


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

    handles = [path.open("rb") for path in images]
    try:
        files = [
            ("image", (path.name, handle, "image/jpeg"))
            for path, handle in zip(images, handles)
        ]
        started = time.monotonic()
        response = requests.post(
            endpoint,
            files=files,
            data={"states": json.dumps(state), "text": PROMPT},
            timeout=timeout,
        )
        latency = time.monotonic() - started
        response.raise_for_status()
    finally:
        for handle in handles:
            handle.close()

    actions = response.json().get("response")
    if not isinstance(actions, list) or not actions:
        raise ValueError("server returned an empty or invalid action chunk")
    if any(not isinstance(step, list) or len(step) != ACTION_DIM for step in actions):
        raise ValueError("server response is not chunk_size x 34")
    return actions, latency


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Request actions and record them; never commands robot hardware."
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:7891/process_frame")
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
