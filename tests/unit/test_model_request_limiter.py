"""本地 MLLM 共享请求门禁的并发回归测试。"""

import asyncio

import pytest

from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter


def test_model_request_limiter_caps_concurrent_slots() -> None:
    async def scenario() -> None:
        limiter = ModelRequestLimiter(2)
        release = asyncio.Event()
        two_requests_entered = asyncio.Event()
        active_requests = 0
        maximum_active_requests = 0

        async def request() -> None:
            nonlocal active_requests, maximum_active_requests
            async with limiter.slot():
                active_requests += 1
                maximum_active_requests = max(maximum_active_requests, active_requests)
                if active_requests == 2:
                    two_requests_entered.set()
                await release.wait()
                active_requests -= 1

        tasks = [asyncio.create_task(request()) for _ in range(5)]
        await asyncio.wait_for(two_requests_entered.wait(), timeout=1)
        assert active_requests == 2
        assert maximum_active_requests == 2

        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert maximum_active_requests == 2

    asyncio.run(scenario())


def test_model_request_limiter_rejects_nonpositive_capacity() -> None:
    with pytest.raises(ValueError, match="必须大于 0"):
        ModelRequestLimiter(0)
