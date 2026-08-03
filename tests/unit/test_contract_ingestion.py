from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from typing import Any

import pytest

from contract_processor.application.schemas.contract_ingestion import (
    ContractReviewConfirmation,
)
from contract_processor.application.use_cases.ingest_reviewed_contract import (
    IngestReviewedContract,
)
from contract_processor.application.workflows.contract_ingestion import (
    ContractIngestionNodeError,
    ContractIngestionWorkflow,
)
from contract_processor.infrastructure.orchestration.contract_ingestion_graph import (
    ContractIngestionGraphFactory,
)
from contract_processor.infrastructure.pdf.document_identity import (
    Sha256DocumentIdentityProvider,
)
from contract_processor.infrastructure.persistence.local_source_document_store import (
    LocalSourceDocumentStore,
)


def _scalar(value: Any, *, status: str = "found") -> dict[str, Any]:
    return {
        "raw_value": None if value is None else str(value),
        "reason": "终审确认",
        "status": status,
        "value": value,
    }


def _confirmation(document_id: str) -> ContractReviewConfirmation:
    return ContractReviewConfirmation.model_validate(
        {
            "document_id": document_id,
            "review": {
                "reviewer": "测试审核员",
                "reviewed_at": "2026-08-02T20:00:00+08:00",
                "comment": "确认入库",
            },
            "result": {
                "document_id": document_id,
                "source_name": "测试合同.pdf",
                "core": {
                    "contract_title": _scalar("设备采购合同"),
                    "contract_parties": _scalar(
                        [
                            {
                                "source_name": "深圳现象光伏科技有限公司",
                                "normalized_name": "深圳现象光伏科技有限公司",
                            },
                            {
                                "source_name": "外部设备有限公司",
                                "normalized_name": "外部设备有限公司",
                            },
                        ]
                    ),
                    "subject_matter": {
                        "status": "found",
                        "properties": {
                            "summary": _scalar("机械臂"),
                            "items": _scalar(
                                [
                                    {
                                        "source_name": "机械臂",
                                        "normalized_name": "机械臂",
                                    }
                                ]
                            ),
                        },
                    },
                    "empty": _scalar(None, status="not_found"),
                },
                "attribute": [],
                "clause": [],
                "abstract": {"sections": {}, "text": "机械臂设备采购合同摘要。"},
                "processing": {
                    "model": "test-model",
                    "prompt_version": "a" * 64,
                    "source_page_count": 1,
                    "core_schema_version": "1",
                    "attribute_schema_version": "1",
                    "clause_schema_version": "1",
                    "summary_schema_version": "1",
                },
            },
        }
    )


class _ConcurrentEmbedding:
    dimensions = 2
    model = "test-embedding"
    instruction_version = "b" * 64
    visual_strategy = "normalized_page_mean_v1"

    def __init__(self) -> None:
        self.text_started = asyncio.Event()
        self.visual_started = asyncio.Event()

    async def probe(self) -> None:
        return None

    async def embed_text_fields(
        self, inputs: dict[str, str]
    ) -> dict[str, list[float]]:
        self.text_started.set()
        await asyncio.wait_for(self.visual_started.wait(), timeout=1)
        return {field: [1.0, 0.0] for field in inputs}

    async def embed_pdf(self, pdf_path: Path) -> tuple[list[float], int]:
        self.visual_started.set()
        await asyncio.wait_for(self.text_started.wait(), timeout=1)
        return [0.0, 1.0], 1

    async def close(self) -> None:
        return None


class _MemoryRepository:
    index_name = "contracts-ingestion-experiment-test"

    def __init__(self, *, raise_after_save: bool = False) -> None:
        self.document: dict[str, Any] | None = None
        self.raise_after_save = raise_after_save

    async def ensure_ready(self) -> str:
        return "created"

    async def save(self, document: dict[str, Any]) -> None:
        self.document = document
        if self.raise_after_save:
            raise ConnectionError("模拟响应在提交后丢失")

    async def get(self, document_id: str) -> dict[str, Any] | None:
        if self.document and self.document["document_id"] == document_id:
            return self.document
        return None

    async def close(self) -> None:
        return None


def _use_case(
    *,
    embedding: Any,
    repository: _MemoryRepository,
    document_root: Path,
) -> IngestReviewedContract:
    workflow = ContractIngestionWorkflow(
        own_company_names=("现象光伏", "现象创新", "phenosolar"),
        identity_provider=Sha256DocumentIdentityProvider(),
        embedding_client=embedding,
        source_document_store=LocalSourceDocumentStore(document_root),
        index_repository=repository,
        clock=lambda: datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )
    return IngestReviewedContract(
        workflow=workflow,
        graph_factory=ContractIngestionGraphFactory(),
        embedding_client=embedding,
        index_repository=repository,
    )


def test_ingestion_graph_runs_text_and_visual_nodes_concurrently(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"pdf-content")
        document_id = hashlib.sha256(pdf.read_bytes()).hexdigest()
        embedding = _ConcurrentEmbedding()
        repository = _MemoryRepository()
        use_case = _use_case(
            embedding=embedding,
            repository=repository,
            document_root=tmp_path / "contracts",
        )

        outcome = await asyncio.wait_for(
            use_case.execute(_confirmation(document_id), pdf), timeout=2
        )

        assert embedding.text_started.is_set()
        assert embedding.visual_started.is_set()
        assert outcome.vector_fields == (
            "abstract_vector",
            "contract_name_vector",
            "document_visual_vector",
            "product_names_vector",
        )
        assert repository.document is not None
        assert "empty" not in repository.document["core_values"]

    asyncio.run(scenario())


def test_source_document_store_is_idempotent_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source.pdf"
        source.write_bytes(b"stable-pdf")
        document_id = hashlib.sha256(source.read_bytes()).hexdigest()
        store = LocalSourceDocumentStore(tmp_path / "contracts")

        first = await store.save(source, document_id)
        second = await store.save(source, document_id)

        assert first.created is True
        assert second.created is False
        assert first.storage_key == f"{document_id}.pdf"
        assert await store.resolve(document_id) == (
            tmp_path / "contracts" / f"{document_id}.pdf"
        )

        (tmp_path / "contracts" / f"{document_id}.pdf").write_bytes(b"corrupt")
        with pytest.raises(RuntimeError, match="内容不一致"):
            await store.save(source, document_id)
        with pytest.raises(RuntimeError, match="内容哈希校验失败"):
            await store.resolve(document_id)

    asyncio.run(scenario())


def test_index_response_loss_is_recovered_by_attempt_id_readback(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"pdf-content")
        document_id = hashlib.sha256(pdf.read_bytes()).hexdigest()
        embedding = _ConcurrentEmbedding()
        repository = _MemoryRepository(raise_after_save=True)
        use_case = _use_case(
            embedding=embedding,
            repository=repository,
            document_root=tmp_path / "contracts",
        )

        outcome = await use_case.execute(_confirmation(document_id), pdf)

        assert outcome.document_id == document_id
        assert repository.document is not None
        assert len(repository.document["ingestion_attempt_id"]) == 32

    asyncio.run(scenario())


def test_embedding_failure_does_not_persist_pdf_or_index(tmp_path: Path) -> None:
    class FailingEmbedding(_ConcurrentEmbedding):
        async def embed_text_fields(
            self, inputs: dict[str, str]
        ) -> dict[str, list[float]]:
            raise RuntimeError("文本向量服务失败")

        async def embed_pdf(self, pdf_path: Path) -> tuple[list[float], int]:
            return [0.0, 1.0], 1

    async def scenario() -> None:
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"pdf-content")
        document_id = hashlib.sha256(pdf.read_bytes()).hexdigest()
        repository = _MemoryRepository()
        use_case = _use_case(
            embedding=FailingEmbedding(),
            repository=repository,
            document_root=tmp_path / "contracts",
        )

        with pytest.raises(ContractIngestionNodeError) as captured:
            await use_case.execute(_confirmation(document_id), pdf)

        assert captured.value.node == "embed_text_fields"
        assert repository.document is None
        assert not (tmp_path / "contracts").exists()

    asyncio.run(scenario())
