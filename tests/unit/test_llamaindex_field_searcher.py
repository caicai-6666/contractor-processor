from __future__ import annotations

import asyncio

from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import FieldDefinition, OutputDefinition
from contract_processor.infrastructure.rag.llamaindex_field_searcher import (
    LlamaIndexFieldSimilaritySearcher,
)


def _definition(field_id: str, name: str, meaning: str) -> FieldDefinition:
    return FieldDefinition(
        field_id=field_id,
        name=name,
        meaning=meaning,
        aliases=(),
        not_meaning=(),
        output=OutputDefinition(
            type="string",
            format=None,
            nullable=True,
        ),
        extraction_rule="只提取合同明确记载的内容。",
        examples=(),
        kind=FieldKind.CORE,
    )


class _FakeFieldEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_field_summary(self, summary: str) -> list[float]:
        self.calls.append(summary)
        if "编号" in summary:
            return [1.0, 0.0]
        return [0.0, 1.0]


def test_field_similarity_uses_batch_local_llamaindex_store() -> None:
    async def scenario() -> None:
        embedding = _FakeFieldEmbeddingClient()
        contract_number = _definition(
            "contract_number", "合同编号", "当前合同自身编号"
        )
        delivery_location = _definition(
            "delivery_location", "交付地点", "货物交付地址"
        )
        searcher = await LlamaIndexFieldSimilaritySearcher.build(
            [contract_number, delivery_location],
            embedding_client=embedding,
        )

        result = await searcher.search("采购订单编号", limit=1)

        assert result == (contract_number,)
        # 两个目录字段和一个查询在当前进程中临时向量化，不产生 ES 读写。
        assert len(embedding.calls) == 3

    asyncio.run(scenario())


def test_empty_field_catalog_returns_without_embedding_query() -> None:
    async def scenario() -> None:
        embedding = _FakeFieldEmbeddingClient()
        searcher = await LlamaIndexFieldSimilaritySearcher.build(
            [],
            embedding_client=embedding,
        )

        assert await searcher.search("任意候选", limit=5) == ()
        assert embedding.calls == []

    asyncio.run(scenario())
