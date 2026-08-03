"""调用独立四节点图完成专家终审合同入库。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from contract_processor.application.ports.contract_ingestion import (
    ContractEmbeddingClient,
    ContractIngestionGraphFactory,
    ContractIndexRepository,
)
from contract_processor.application.schemas.contract_ingestion import (
    ContractIngestionOutcome,
    ContractReviewConfirmation,
)
from contract_processor.application.workflows.contract_ingestion import (
    ContractIngestionWorkflow,
)


class IngestReviewedContract:
    """FastAPI、Worker 和实验可共同调用的正式异步用例。"""

    def __init__(
        self,
        *,
        workflow: ContractIngestionWorkflow,
        graph_factory: ContractIngestionGraphFactory,
        embedding_client: ContractEmbeddingClient,
        index_repository: ContractIndexRepository,
    ) -> None:
        self._embedding_client = embedding_client
        self._index_repository = index_repository
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._graph = graph_factory.build_contract_ingestion(
            prepare_ingestion=workflow.prepare_ingestion,
            embed_text_fields=workflow.embed_text_fields,
            embed_pdf_visual=workflow.embed_pdf_visual,
            persist_contract=workflow.persist_contract,
        )

    async def initialize(self) -> str:
        """服务启动或首次调用时探测依赖并校验正式索引。"""

        if self._initialized:
            return "validated"
        async with self._initialize_lock:
            if self._initialized:
                return "validated"
            await self._embedding_client.probe()
            status = await self._index_repository.ensure_ready()
            self._initialized = True
            return status

    async def execute(
        self,
        confirmation: ContractReviewConfirmation,
        source_pdf: Path,
    ) -> ContractIngestionOutcome:
        await self.initialize()
        state = await self._graph.ainvoke(
            {
                "confirmation": confirmation,
                "source_pdf": source_pdf,
            }
        )
        outcome = state.get("outcome")
        if not isinstance(outcome, ContractIngestionOutcome):
            raise RuntimeError("入库子图结束但没有返回 ContractIngestionOutcome。")
        return outcome

    async def close(self) -> None:
        await asyncio.gather(
            self._embedding_client.close(),
            self._index_repository.close(),
        )
