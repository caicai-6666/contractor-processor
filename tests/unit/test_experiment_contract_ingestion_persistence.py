from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.contract_ingestion_persistence.clear_index import (
    clear_index_documents,
    validate_clear_target,
)
from experiments.contract_ingestion_persistence.embedding import (
    fuse_page_embeddings,
)
from experiments.contract_ingestion_persistence.projection import (
    build_search_projection,
    is_own_company_name,
    parse_own_company_names,
)
from experiments.contract_ingestion_persistence.rebuild_empty_index import (
    rebuild_empty_experiment_index,
)
from experiments.contract_ingestion_persistence.run import (
    recreate_experiment_index,
    validate_recreate_target,
)
from experiments.contract_ingestion_persistence.vector_store import (
    CHINESE_TEXT_FIELDS,
    VECTOR_FIELDS,
    Elasticsearch9ContractVectorStore,
    build_contract_node,
    build_ingestion_mapping,
    find_mapping_incompatibilities,
)
from contract_processor.interfaces.api.schemas.contracts import (
    ContractReviewConfirmation,
)
from contract_processor.application.use_cases.ingest_reviewed_contract import (
    IngestReviewedContract,
)
from contract_processor.application.workflows.contract_ingestion import (
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
        "reason": "测试理由",
        "status": status,
        "value": value,
    }


def _confirmation(document_id: str) -> ContractReviewConfirmation:
    payload = {
        "document_id": document_id,
        "review": {
            "reviewer": "Tester",
            "reviewed_at": "2026-08-02T20:00:00+08:00",
            "comment": "测试终审",
        },
        "result": {
            "document_id": document_id,
            "source_name": "contract.pdf",
            "core": {
                "contract_title": _scalar("采购合同"),
                "contract_parties": _scalar(
                    [
                        {
                            "source_name": "深圳现象光伏科技有限公司",
                            "normalized_name": "深圳现象光伏科技有限公司",
                        },
                        {
                            "source_name": "外部科技有限公司",
                            "normalized_name": "外部科技有限公司",
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
                                    "source_name": "定制机械臂",
                                    "normalized_name": "机械臂",
                                    "brand": None,
                                }
                            ]
                        ),
                        "unused": _scalar(None, status="not_found"),
                    },
                },
                "empty_field": _scalar(None, status="not_found"),
            },
            "attribute": [
                {"field_id": "project_number", **_scalar("P-001")},
                {"field_id": "empty_attribute", **_scalar(None, status="not_found")},
            ],
            "clause": [
                {
                    "clause_number": "1",
                    "heading": "交付",
                    "category": "delivery_or_service",
                    "source_text": "应于十日内交付。",
                    "page_refs": [1],
                }
            ],
            "abstract": {"sections": {}, "text": "这是一份机械臂采购合同。"},
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
    return ContractReviewConfirmation.model_validate(payload)


def test_company_aliases_and_sparse_projection() -> None:
    aliases = parse_own_company_names('["现象光伏", "现象创新", "phenosolar"]')
    assert is_own_company_name("深圳现象光伏科技有限公司", aliases)
    assert is_own_company_name("Phenosolar Limited", aliases)
    assert not is_own_company_name("NotPhenosolar Limited", aliases)

    confirmation = _confirmation("a" * 64)
    projection = build_search_projection(
        confirmation,
        own_company_names=aliases,
    )

    assert projection.contract_name == "采购合同"
    assert projection.counterparty_names == ("外部科技有限公司",)
    assert projection.product_names == ("机械臂",)
    assert projection.counterparty_resolution_status == "resolved"
    assert "empty_field" not in projection.document["reviewed_result"]["core"]
    assert "unused" not in projection.document["core_values"]["subject_matter"]
    assert projection.document["attribute_values"] == {"project_number": "P-001"}
    assert set(projection.text_embedding_inputs) == {
        "contract_name_vector",
        "product_names_vector",
        "abstract_vector",
    }
    assert "counterparty_name_vector" not in projection.text_embedding_inputs


def test_page_embedding_fusion_is_normalized() -> None:
    fused = fuse_page_embeddings([[3.0, 0.0], [0.0, 4.0]])
    assert fused == pytest.approx([math.sqrt(0.5), math.sqrt(0.5)])
    assert math.sqrt(sum(value * value for value in fused)) == pytest.approx(1.0)


class _FakeClearIndices:
    async def exists(self, *, index: str) -> bool:
        return True


class _FakeClearClient:
    def __init__(self, documents: int) -> None:
        self.documents = documents
        self.delete_calls = 0
        self.indices = _FakeClearIndices()

    async def count(self, *, index: str) -> dict[str, int]:
        return {"count": self.documents}

    async def delete_by_query(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls += 1
        deleted = self.documents
        self.documents = 0
        return {"deleted": deleted, "version_conflicts": 0, "failures": []}


def test_clear_index_defaults_to_preview_without_deletion() -> None:
    async def scenario() -> None:
        client = _FakeClearClient(documents=4)
        outcome = await clear_index_documents(
            client=client,
            index_name="contracts-ingestion-experiment-v1",
            production_index_name="contracts-v1",
            execute=False,
            confirmed_index_name=None,
        )

        assert not outcome.executed
        assert outcome.documents_before == 4
        assert outcome.documents_after == 4
        assert client.delete_calls == 0

    asyncio.run(scenario())


def test_clear_index_requires_exact_confirmation_and_preserves_mapping() -> None:
    async def scenario() -> None:
        client = _FakeClearClient(documents=4)
        with pytest.raises(RuntimeError, match="完全一致"):
            await clear_index_documents(
                client=client,
                index_name="contracts-ingestion-experiment-v1",
                production_index_name="contracts-v1",
                execute=True,
                confirmed_index_name="wrong-index",
            )
        outcome = await clear_index_documents(
            client=client,
            index_name="contracts-ingestion-experiment-v1",
            production_index_name="contracts-v1",
            execute=True,
            confirmed_index_name="contracts-ingestion-experiment-v1",
        )

        assert outcome.executed
        assert outcome.deleted == 4
        assert outcome.documents_after == 0
        assert outcome.mapping_preserved
        assert client.delete_calls == 1

    asyncio.run(scenario())


def test_clear_index_refuses_production_or_unmarked_index() -> None:
    with pytest.raises(RuntimeError, match="正式合同索引"):
        validate_clear_target(
            index_name="contracts-v1",
            production_index_name="contracts-v1",
            execute=True,
            confirmed_index_name="contracts-v1",
        )
    with pytest.raises(RuntimeError, match="安全标记"):
        validate_clear_target(
            index_name="contracts-staging-v1",
            production_index_name="contracts-v1",
            execute=True,
            confirmed_index_name="contracts-staging-v1",
        )


class _FakeIndices:
    def __init__(self, owner: "_FakeElasticsearch") -> None:
        self._owner = owner

    async def exists(self, *, index: str) -> bool:
        return self._owner.mapping is not None

    async def create(
        self,
        *,
        index: str,
        mappings: dict[str, Any],
        settings: dict[str, Any],
        wait_for_active_shards: int,
    ) -> None:
        assert wait_for_active_shards == 0
        self._owner.mapping = mappings
        self._owner.created += 1

    async def get_mapping(self, *, index: str) -> dict[str, Any]:
        return {index: {"mappings": self._owner.mapping}}

    async def put_settings(
        self, *, index: str, settings: dict[str, Any]
    ) -> None:
        self._owner.write_blocked = bool(settings["index.blocks.write"])

    async def delete(self, *, index: str) -> None:
        self._owner.mapping = None
        self._owner.write_blocked = False
        self._owner.deleted += 1


class _FakeElasticsearch:
    def __init__(self) -> None:
        self.mapping: dict[str, Any] | None = None
        self.document: dict[str, Any] | None = None
        self.document_id: str | None = None
        self.documents = 0
        self.created = 0
        self.deleted = 0
        self.write_blocked = False
        self.indices = _FakeIndices(self)
        self.cluster = _FakeCluster()

    async def count(self, *, index: str) -> dict[str, int]:
        return {"count": self.documents}

    async def index(
        self,
        *,
        index: str,
        id: str,
        document: dict[str, Any],
        refresh: str,
    ) -> None:
        self.document = document
        self.document_id = id

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        assert self.document is not None
        return {
            "hits": {
                "hits": [
                    {
                        "_id": self.document_id,
                        "_score": 1.0,
                        "_source": {
                            key: value
                            for key, value in self.document.items()
                            if key not in VECTOR_FIELDS
                        },
                    }
                ]
            }
        }


class _FakeCluster:
    async def health(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "yellow", "timed_out": False}


def test_llamaindex_vector_store_writes_one_multi_vector_document() -> None:
    async def scenario() -> None:
        client = _FakeElasticsearch()
        store = Elasticsearch9ContractVectorStore(
            client=client,
            index_name="test-index",
            dimensions=2,
        )
        document = {
            "document_id": "a" * 64,
            "abstract": {"text": "摘要"},
            "abstract_vector": [1.0, 0.0],
            "document_visual_vector": [0.0, 1.0],
        }
        node = build_contract_node(document)
        ids = await store.async_add([node])

        assert ids == ["a" * 64]
        assert client.document == document
        assert client.mapping == build_ingestion_mapping(2)

    asyncio.run(scenario())


def test_ingestion_mapping_uses_smartcn_for_all_chinese_text_fields() -> None:
    mapping = build_ingestion_mapping(2)

    for dotted_path in CHINESE_TEXT_FIELDS:
        field_mapping: dict[str, Any] = mapping
        for part in dotted_path.split("."):
            field_mapping = field_mapping["properties"][part]
        assert field_mapping["analyzer"] == "smartcn"
        assert field_mapping["search_analyzer"] == "smartcn"
    assert not find_mapping_incompatibilities(mapping, 2)


def test_ingestion_mapping_gate_rejects_default_text_analyzer() -> None:
    mapping = build_ingestion_mapping(2)
    del mapping["properties"]["abstract"]["properties"]["text"]["analyzer"]

    incompatibilities = find_mapping_incompatibilities(mapping, 2)

    assert "abstract.text.analyzer=None，要求 'smartcn'" in incompatibilities


def test_rebuild_empty_experiment_index_replaces_mapping_safely() -> None:
    async def scenario() -> None:
        client = _FakeElasticsearch()
        old_mapping = build_ingestion_mapping(2)
        del old_mapping["properties"]["source_name"]["analyzer"]
        client.mapping = old_mapping

        outcome = await rebuild_empty_experiment_index(
            client=client,
            index_name="contracts-ingestion-experiment-v1",
            production_index_name="contracts-v1",
            dimensions=2,
            execute=True,
            confirmed_index_name="contracts-ingestion-experiment-v1",
        )

        assert outcome.executed
        assert not outcome.mapping_compatible_before
        assert outcome.mapping_status_after == "created"
        assert client.deleted == 1
        assert client.created == 1
        assert client.mapping == build_ingestion_mapping(2)
        assert not client.write_blocked

    asyncio.run(scenario())


def test_rebuild_experiment_index_refuses_nonempty_index() -> None:
    async def scenario() -> None:
        client = _FakeElasticsearch()
        client.mapping = build_ingestion_mapping(2)
        client.documents = 1

        with pytest.raises(RuntimeError, match="仍有 1 条文档"):
            await rebuild_empty_experiment_index(
                client=client,
                index_name="contracts-ingestion-experiment-v1",
                production_index_name="contracts-v1",
                dimensions=2,
                execute=True,
                confirmed_index_name="contracts-ingestion-experiment-v1",
            )

        assert client.deleted == 0
        assert not client.write_blocked

    asyncio.run(scenario())


def test_rebuild_experiment_index_recovers_missing_index() -> None:
    async def scenario() -> None:
        client = _FakeElasticsearch()

        outcome = await rebuild_empty_experiment_index(
            client=client,
            index_name="contracts-ingestion-experiment-v1",
            production_index_name="contracts-v1",
            dimensions=2,
            execute=True,
            confirmed_index_name="contracts-ingestion-experiment-v1",
        )

        assert outcome.mapping_status_after == "created"
        assert client.deleted == 0
        assert client.created == 1
        assert client.mapping == build_ingestion_mapping(2)

    asyncio.run(scenario())


class _FakeEmbeddingClient:
    dimensions = 2
    model = "test-embedding"
    instruction_version = "a" * 64
    visual_strategy = "normalized_page_mean_v1"

    async def probe(self) -> None:
        return None

    async def embed_text_fields(self, inputs: dict[str, str]) -> dict[str, list[float]]:
        return {field: [1.0, 0.0] for field in inputs}

    async def embed_pdf(self, pdf_path: Path) -> tuple[list[float], int]:
        return [0.0, 1.0], 1

    async def close(self) -> None:
        return None


class _FakeIndexRepository:
    index_name = "contracts-ingestion-experiment-test"

    def __init__(self) -> None:
        self.document: dict[str, Any] | None = None

    async def ensure_ready(self) -> str:
        return "created"

    async def save(self, document: dict[str, Any]) -> None:
        self.document = document

    async def get(self, document_id: str) -> dict[str, Any] | None:
        if self.document and self.document["document_id"] == document_id:
            return self.document
        return None

    async def close(self) -> None:
        return None


def test_formal_four_node_graph_persists_pdf_and_index_document(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pdf_path = tmp_path / "contract.pdf"
        pdf_path.write_bytes(b"test-pdf-bytes")
        document_id = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        embedding = _FakeEmbeddingClient()
        repository = _FakeIndexRepository()
        document_store = LocalSourceDocumentStore(tmp_path / "contracts")
        workflow = ContractIngestionWorkflow(
            own_company_names=("现象光伏",),
            identity_provider=Sha256DocumentIdentityProvider(),
            embedding_client=embedding,
            source_document_store=document_store,
            index_repository=repository,
        )
        use_case = IngestReviewedContract(
            workflow=workflow,
            graph_factory=ContractIngestionGraphFactory(),
            embedding_client=embedding,
            index_repository=repository,
        )

        outcome = await use_case.execute(_confirmation(document_id), pdf_path)

        assert outcome.document_id == document_id
        assert outcome.vector_fields == tuple(sorted(VECTOR_FIELDS))
        assert outcome.source_document.created
        assert (tmp_path / "contracts" / f"{document_id}.pdf").read_bytes() == (
            b"test-pdf-bytes"
        )
        assert repository.document is not None
        assert repository.document["counterparty_names"] == ["外部科技有限公司"]
        assert repository.document["source_document"]["storage_key"] == (
            f"{document_id}.pdf"
        )

    asyncio.run(scenario())


def test_ingestion_acceptance_always_recreates_only_experiment_index() -> None:
    with pytest.raises(RuntimeError, match="正式合同索引"):
        validate_recreate_target(
            index_name="contracts-v1",
            production_index_name="contracts-v1",
        )
    with pytest.raises(RuntimeError, match="experiment"):
        validate_recreate_target(
            index_name="contracts-staging-v1",
            production_index_name="contracts-v1",
        )

    async def scenario() -> None:
        client = _FakeElasticsearch()
        client.mapping = build_ingestion_mapping(2)
        client.documents = 5

        existed, status = await recreate_experiment_index(
            client=client,
            index_name="contracts-ingestion-experiment-v1",
            production_index_name="contracts-v1",
            dimensions=2,
        )

        assert existed is True
        assert status == "created"
        assert client.deleted == 1
        assert client.created == 1
        assert client.mapping == build_ingestion_mapping(2)

    asyncio.run(scenario())
