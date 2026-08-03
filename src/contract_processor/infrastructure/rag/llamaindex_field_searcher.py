"""基于 LlamaIndex SimpleVectorStore 的批次内字段相似度召回。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from llama_index.core.schema import TextNode
from llama_index.core.vector_stores.simple import SimpleVectorStore
from llama_index.core.vector_stores.types import VectorStoreQuery

from contract_processor.application.ports.contracts import (
    FieldSummaryEmbeddingClient,
)
from contract_processor.domain.models import FieldDefinition


class LlamaIndexFieldSimilaritySearcher:
    """一次 discovery 批次建立一次内存索引，进程结束后直接释放。"""

    def __init__(
        self,
        *,
        definitions_by_node_id: dict[str, FieldDefinition],
        vector_store: SimpleVectorStore,
        embedding_client: FieldSummaryEmbeddingClient,
        dimensions: int | None,
    ) -> None:
        self._definitions_by_node_id = definitions_by_node_id
        self._vector_store = vector_store
        self._embedding_client = embedding_client
        self._dimensions = dimensions

    @classmethod
    async def build(
        cls,
        definitions: Sequence[FieldDefinition],
        *,
        embedding_client: FieldSummaryEmbeddingClient,
    ) -> "LlamaIndexFieldSimilaritySearcher":
        """向量化当前批次字段目录，不读取或写入 Elasticsearch。"""

        definitions_by_node_id: dict[str, FieldDefinition] = {}
        for definition in definitions:
            node_id = f"{definition.kind.value}:{definition.field_id}"
            if node_id in definitions_by_node_id:
                raise ValueError(f"字段定义重复：{node_id}")
            definitions_by_node_id[node_id] = definition

        store = SimpleVectorStore()
        if not definitions_by_node_id:
            return cls(
                definitions_by_node_id={},
                vector_store=store,
                embedding_client=embedding_client,
                dimensions=None,
            )
        node_ids = tuple(definitions_by_node_id)
        embeddings = await asyncio.gather(
            *(
                embedding_client.embed_field_summary(
                    definitions_by_node_id[node_id].summary()
                )
                for node_id in node_ids
            )
        )
        dimensions = len(embeddings[0])
        if dimensions < 1:
            raise ValueError("字段摘要 Embedding 返回空向量。")
        if any(len(embedding) != dimensions for embedding in embeddings):
            raise ValueError("字段摘要 Embedding 维度不一致。")
        nodes = [
            TextNode(
                id_=node_id,
                text=definitions_by_node_id[node_id].summary(),
                embedding=[float(value) for value in embedding],
            )
            for node_id, embedding in zip(node_ids, embeddings, strict=True)
        ]
        await store.async_add(nodes)
        return cls(
            definitions_by_node_id=definitions_by_node_id,
            vector_store=store,
            embedding_client=embedding_client,
            dimensions=dimensions,
        )

    async def search(
        self, summary: str, *, limit: int
    ) -> Sequence[FieldDefinition]:
        """按余弦相似度降序返回本批次字段候选。"""

        if limit < 1:
            raise ValueError("字段召回 limit 必须大于 0。")
        if not summary.strip():
            raise ValueError("候选字段摘要不能为空。")
        if not self._definitions_by_node_id:
            return ()
        embedding = await self._embedding_client.embed_field_summary(summary.strip())
        if len(embedding) != self._dimensions:
            raise ValueError(
                f"查询向量维度为 {len(embedding)}，字段索引维度为 {self._dimensions}。"
            )
        result = await self._vector_store.aquery(
            VectorStoreQuery(
                query_embedding=[float(value) for value in embedding],
                similarity_top_k=min(limit, len(self._definitions_by_node_id)),
            )
        )
        return tuple(
            self._definitions_by_node_id[node_id]
            for node_id in result.ids or []
        )
