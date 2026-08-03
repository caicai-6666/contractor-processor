"""独立终审入库图的四个业务节点。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, TypedDict
from uuid import uuid4

from contract_processor.application.ports.contract_ingestion import (
    ContractEmbeddingClient,
    ContractIndexRepository,
    DocumentIdentityProvider,
    SourceDocumentStore,
)
from contract_processor.application.schemas.contract_ingestion import (
    ContractIngestionOutcome,
    ContractReviewConfirmation,
    ContractSearchProjection,
    StoredSourceDocument,
)
from contract_processor.application.services.contract_ingestion_projection import (
    build_contract_search_projection,
)


class ContractIngestionState(TypedDict, total=False):
    """并行分支分别写入独立键，避免 LangGraph 并发更新冲突。"""

    confirmation: ContractReviewConfirmation
    source_pdf: Path
    projection: ContractSearchProjection
    text_vectors: dict[str, list[float]]
    visual_vector: list[float]
    visual_page_count: int
    outcome: ContractIngestionOutcome


class ContractIngestionNodeError(RuntimeError):
    """保留失败节点，供未来 API 和实验返回稳定诊断。"""

    def __init__(self, node: str, cause: Exception) -> None:
        super().__init__(f"入库节点 {node} 失败：{cause}")
        self.node = node
        self.cause_type = type(cause).__name__


class ContractIngestionWorkflow:
    """节点只表达业务动作，图拓扑由基础设施层工厂负责。"""

    def __init__(
        self,
        *,
        own_company_names: tuple[str, ...],
        identity_provider: DocumentIdentityProvider,
        embedding_client: ContractEmbeddingClient,
        source_document_store: SourceDocumentStore,
        index_repository: ContractIndexRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._own_company_names = own_company_names
        self._identity_provider = identity_provider
        self._embedding_client = embedding_client
        self._source_document_store = source_document_store
        self._index_repository = index_repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def prepare_ingestion(
        self, state: ContractIngestionState
    ) -> dict[str, Any]:
        """节点一：校验 PDF 身份并构造终审稀疏投影。"""

        try:
            confirmation = state["confirmation"]
            source_pdf = state["source_pdf"]
            actual_document_id = await self._identity_provider.compute(source_pdf)
            if actual_document_id != confirmation.document_id:
                raise ValueError(
                    "源 PDF SHA-256 与终审包络 document_id 不一致，拒绝入库。"
                )
            projection = build_contract_search_projection(
                confirmation,
                own_company_names=self._own_company_names,
            )
            return {"projection": projection}
        except ContractIngestionNodeError:
            raise
        except Exception as error:
            raise ContractIngestionNodeError("prepare_ingestion", error) from error

    async def embed_text_fields(
        self, state: ContractIngestionState
    ) -> dict[str, Any]:
        """节点二：并发生成所有存在值的字段级文本向量。"""

        try:
            projection = state["projection"]
            vectors = await self._embedding_client.embed_text_fields(
                projection.text_embedding_inputs
            )
            return {"text_vectors": vectors}
        except ContractIngestionNodeError:
            raise
        except Exception as error:
            raise ContractIngestionNodeError("embed_text_fields", error) from error

    async def embed_pdf_visual(
        self, state: ContractIngestionState
    ) -> dict[str, Any]:
        """节点三：逐页生成并融合合同视觉向量。"""

        try:
            vector, page_count = await self._embedding_client.embed_pdf(
                state["source_pdf"]
            )
            return {
                "visual_vector": vector,
                "visual_page_count": page_count,
            }
        except ContractIngestionNodeError:
            raise
        except Exception as error:
            raise ContractIngestionNodeError("embed_pdf_visual", error) from error

    async def persist_contract(
        self, state: ContractIngestionState
    ) -> dict[str, Any]:
        """节点四：先原子保存 PDF，再以单文档形式写入全部元数据和向量。"""

        try:
            confirmation = state["confirmation"]
            projection = state["projection"]
            text_vectors = state["text_vectors"]
            visual_vector = state["visual_vector"]
            page_count = state["visual_page_count"]
            source_document = await self._source_document_store.save(
                state["source_pdf"], confirmation.document_id
            )

            ingested_at = self._clock()
            if ingested_at.tzinfo is None or ingested_at.utcoffset() is None:
                raise ValueError("入库时钟必须返回带时区时间。")
            ingestion_attempt_id = uuid4().hex
            vectors = {**text_vectors, "document_visual_vector": visual_vector}
            document = deepcopy(projection.document)
            document.update(deepcopy(vectors))
            document["source_document"] = source_document.as_metadata()
            document["vectorization"] = {
                "model": self._embedding_client.model,
                "instruction_version": self._embedding_client.instruction_version,
                "dimensions": self._embedding_client.dimensions,
                "normalized": True,
                "visual_strategy": self._embedding_client.visual_strategy,
                "visual_page_count": page_count,
                "embedded_fields": sorted(vectors),
            }
            document["ingestion_attempt_id"] = ingestion_attempt_id
            document["ingested_at"] = ingested_at.isoformat()

            try:
                await self._index_repository.save(document)
            except Exception:
                # 网络异常可能发生在 ES 已提交之后；按本次 attempt_id 回读，避免把成功
                # 写入误报为失败。PDF 使用内容寻址，保留孤儿文件比误删更安全且可幂等复用。
                stored = await self._index_repository.get(confirmation.document_id)
                if not stored or stored.get("ingestion_attempt_id") != ingestion_attempt_id:
                    raise

            stored = await self._index_repository.get(confirmation.document_id)
            if not stored or stored.get("ingestion_attempt_id") != ingestion_attempt_id:
                raise RuntimeError("Elasticsearch 按 document_id 回读校验失败。")
            stored_source = stored.get("source_document")
            if not isinstance(stored_source, dict) or (
                stored_source.get("storage_key") != source_document.storage_key
            ):
                raise RuntimeError("Elasticsearch 中的 PDF 存储键与落盘结果不一致。")

            outcome = ContractIngestionOutcome(
                document_id=confirmation.document_id,
                source_name=confirmation.result.source_name,
                source_document=source_document,
                cleaning=projection.metrics,
                vector_fields=tuple(sorted(vectors)),
                vector_dimensions=self._embedding_client.dimensions,
                visual_page_count=page_count,
                visual_strategy=self._embedding_client.visual_strategy,
                counterparty_resolution_status=(
                    projection.counterparty_resolution_status
                ),
                ingested_at=ingested_at,
                index_name=self._index_repository.index_name,
            )
            return {"outcome": outcome}
        except ContractIngestionNodeError:
            raise
        except Exception as error:
            raise ContractIngestionNodeError("persist_contract", error) from error
