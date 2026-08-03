"""未来 FastAPI 路由使用的依赖组装函数；当前不创建路由。"""

from collections.abc import AsyncIterator
from pathlib import Path

from contract_processor.application.use_cases.ingest_reviewed_contract import (
    IngestReviewedContract,
)
from contract_processor.application.use_cases.process_contract import ProcessContract
from contract_processor.bootstrap.container import (
    build_ingest_reviewed_contract,
    build_process_contract,
)


async def process_contract_dependency(project_root: Path) -> ProcessContract:
    """返回与 IDE 入口完全相同的应用用例。"""

    return await build_process_contract(project_root)


async def ingest_reviewed_contract_dependency(
    project_root: Path,
) -> AsyncIterator[IngestReviewedContract]:
    """为未来请求托管入库用例客户端的完整异步生命周期。"""

    use_case = await build_ingest_reviewed_contract(project_root)
    try:
        yield use_case
    finally:
        await use_case.close()
