"""G2真实动作统一的简短人工确认与倒计时。

用途：让所有现场动作使用相同的``ok/OK``确认体验。
输入：动作说明、可选预先提供的确认文本以及固定倒计时秒数。
输出：确认成功后返回；其他输入抛出``PermissionError``且不执行动作。
方法：忽略大小写和首尾空格，只接受ok，然后逐秒打印5秒倒计时。
"""

from __future__ import annotations

import time
from collections.abc import Callable


def confirm_ok_and_countdown(
    action: str,
    *,
    supplied: str = "",
    seconds: int = 5,
    input_fn: Callable[[str], str] = input,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    answer = supplied or input_fn(
        f"即将执行真实动作：{action}。确认现场安全后输入 ok："
    )
    if answer.strip().lower() != "ok":
        raise PermissionError("未输入ok，已取消真实动作")
    print("确认成功，5秒后开始；专人握住物理急停。", flush=True)
    for remaining in range(seconds, 0, -1):
        print(f"{remaining}...", flush=True)
        sleep_fn(1.0)
