#!/usr/bin/env python3
"""验证当前容器在指定GPU集合上的NCCL进程组和all-reduce。

本脚本不加载模型或数据，不写checkpoint。使用``CUDA_VISIBLE_DEVICES``选择
不会干扰其他用户的GPU，再通过``torchrun``启动。本文件属于G2项目定制代码。
"""

from __future__ import annotations

import os

import torch
import torch.distributed as dist


def main() -> None:
    dist.init_process_group(backend="nccl")
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)

    value = torch.tensor(float(rank + 1), device=f"cuda:{local_rank}")
    dist.all_reduce(value)
    expected = world_size * (world_size + 1) / 2
    if value.item() != expected:
        raise RuntimeError(
            f"rank={rank}: all_reduce={value.item()} expected={expected}"
        )

    print(
        f"rank={rank}/{world_size} "
        f"device={torch.cuda.get_device_name(local_rank)} "
        f"all_reduce={value.item():.0f}",
        flush=True,
    )
    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
