"""异步调用链中的阻塞函数隔离工具。"""

from __future__ import annotations

import asyncio
import atexit
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar


ResultT = TypeVar("ResultT")
_MAX_BLOCKING_WORKERS = min(32, (os.cpu_count() or 1) + 4)
_BLOCKING_EXECUTOR = ThreadPoolExecutor(
    max_workers=_MAX_BLOCKING_WORKERS,
    thread_name_prefix="contract-blocking",
)


def _shutdown_blocking_executor() -> None:
    """进程退出时停止接收新任务，并回收共享工作线程。"""

    _BLOCKING_EXECUTOR.shutdown(wait=True, cancel_futures=True)


atexit.register(_shutdown_blocking_executor)


async def run_blocking(
    function: Callable[..., ResultT], /, *args: Any, **kwargs: Any
) -> ResultT:
    """在线程中运行阻塞调用，不阻塞当前事件循环。

    项目不使用事件循环的隐式默认线程池，而是复用进程级有界执行器；这样既限制
    并发工作线程数量，也避免每次配置读取或文件哈希都创建新线程。
    """

    call = partial(function, *args, **kwargs)
    future = _BLOCKING_EXECUTOR.submit(call)
    try:
        # 部分受限容器不允许工作线程通过事件循环的 self-pipe 唤醒主线程；短间隔
        # 协作式轮询在普通部署和受限部署中行为一致，同时保持事件循环可调度。
        while not future.done():
            await asyncio.sleep(0.001)
        return future.result()
    except asyncio.CancelledError:
        # 已排队但尚未执行的任务可立即取消；运行中的系统调用由线程自行结束。
        future.cancel()
        raise
