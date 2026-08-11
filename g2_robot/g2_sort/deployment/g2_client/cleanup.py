"""G2运行资源的有界清理工具。

用途：避免GDK或网络对象的原生close调用卡住整个退出流程。
输入：资源名称、close回调和每一步允许等待的最大时间。
输出：单步成功布尔值及清理日志；批量接口依次关闭非空资源。
方法：每个close在daemon线程执行，通过Event等待，超时或异常后继续下一项。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


def run_cleanup_step(
    label: str,
    callback: Callable[[], None],
    timeout_seconds: float = 1.0,
) -> bool:
    """Run one cleanup action without allowing a native SDK hang to block all."""
    done = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(
        target=worker,
        name=f"cleanup-{label.replace(' ', '-')}",
        daemon=True,
    )
    thread.start()
    if not done.wait(max(0.0, timeout_seconds)):
        logger.warning("Cleanup step timed out: %s", label)
        return False
    if errors:
        logger.warning("Cleanup step failed: %s (%s)", label, errors[0])
        return False
    return True


def close_resources(
    resources: Iterable[tuple[str, object | None]],
    timeout_seconds: float = 1.0,
) -> None:
    """Call ``close`` on each non-null resource with a bounded wait."""
    for label, resource in resources:
        if resource is None:
            continue
        close = getattr(resource, "close", None)
        if callable(close):
            run_cleanup_step(label, close, timeout_seconds)
