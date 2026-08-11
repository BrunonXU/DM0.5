#!/usr/bin/env python3
"""DM0.5 G2运行时的兼容启动入口。

用途：保留原来的``g2_sdk_client.py``启动文件名，并转发到解耦后的应用。
输入：命令行参数，例如mode、DM0.5 endpoint、超时和日志路径。
输出：应用退出码；本文件本身不读取相机、不请求模型，也不发送servo。
方法：补齐源码树导入路径后调用``g2_client.app.main``，所有实现均在子包中。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Support ``python g2_sort/deployment/g2_sdk_client.py`` from a source tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from g2_sort.deployment.g2_client.app import main


if __name__ == "__main__":
    raise SystemExit(main())
