"""同一运行时内本地 MLLM 请求的全局并发门禁。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ModelRequestLimiter:
    """限制一个合同处理运行时同时发往 MLLM 的请求数。

    该门禁位于共享 PDF 上下文中，而非某个具体抽取服务中。这样 Core、Clause、Abstract
    及未来 Attribute 即使由不同 LangGraph 分支调用，也会竞争同一组配额，避免业务分支
    并发后演变为无界的 vLLM 请求并发。
    """

    def __init__(self, max_concurrent_requests: int) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests 必须大于 0。")
        self._semaphore = asyncio.BoundedSemaphore(max_concurrent_requests)
        self.max_concurrent_requests = max_concurrent_requests

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """获取一个模型请求配额，并在请求结束、异常或取消时可靠归还。"""

        async with self._semaphore:
            yield
