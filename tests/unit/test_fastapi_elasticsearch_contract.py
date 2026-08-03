import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from contract_processor.application.schemas.contract_processing import (
    ContractAbstract,
    ContractProcessingResult,
    ProcessingMetadata,
)
from contract_processor.infrastructure.persistence.elasticsearch_contract_index import (
    VECTOR_FIELDS,
    Elasticsearch9ContractVectorStore,
    ElasticsearchContractIndexRepository,
    ElasticsearchMappingFactory,
)
from contract_processor.interfaces.api.schemas.contracts import (
    ContractProcessResponse,
    ContractReviewConfirmation,
    ContractReviewTrace,
)


ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_ID = "a" * 64


def result() -> ContractProcessingResult:
    return ContractProcessingResult(
        document_id=DOCUMENT_ID,
        source_name="contract.pdf",
        core={},
        attribute=[],
        clause=[],
        abstract=ContractAbstract(sections={}, text="摘要"),
        processing=ProcessingMetadata(
            model="mllm",
            prompt_version="f" * 64,
            source_page_count=1,
            core_schema_version="1",
            attribute_schema_version="0.1",
            clause_schema_version="1",
            summary_schema_version="1",
        ),
    )


def test_fastapi_response_and_confirmation_share_document_protocol() -> None:
    response = ContractProcessResponse.model_validate(result().model_dump())
    confirmation = ContractReviewConfirmation(
        document_id=DOCUMENT_ID,
        review=ContractReviewTrace(
            reviewer="测试审核员",
            reviewed_at=datetime.now(UTC),
            comment="测试终审确认",
        ),
        result=response,
    )

    confirmation.validate_identity()
    mismatched = confirmation.model_copy(update={"document_id": "b" * 64})
    with pytest.raises(ValueError, match="document_id"):
        mismatched.validate_identity()

    with pytest.raises(ValidationError, match="document_id"):
        ContractReviewConfirmation(
            document_id="b" * 64,
            review=confirmation.review,
            result=response,
        )


def test_elasticsearch_mapping_is_generated_from_core_catalog() -> None:
    mapping = asyncio.run(
        ElasticsearchMappingFactory(ROOT / "data/definitions/core.yaml").build(
            vector_dimensions=2560
        )
    )
    properties = mapping["properties"]

    assert mapping["dynamic"] == "strict"
    assert "mode" not in properties
    assert properties["core_values"]["type"] == "flattened"
    assert properties["attribute_values"]["type"] == "flattened"
    assert properties["reviewed_result"]["enabled"] is False
    assert properties["source_document"]["properties"]["storage_key"]["type"] == (
        "keyword"
    )
    assert properties["clause"]["type"] == "nested"
    assert properties["clause"]["properties"]["heading"]["analyzer"] == "smartcn"
    assert (
        properties["clause"]["properties"]["source_text"]["search_analyzer"]
        == "smartcn"
    )
    assert properties["abstract"]["properties"]["text"]["analyzer"] == "smartcn"
    assert properties["counterparty_names"]["analyzer"] == "smartcn"
    assert "counterparty_name_vector" not in properties
    for field in VECTOR_FIELDS:
        assert properties[field] == {
            "type": "dense_vector",
            "dims": 2560,
            "index": True,
            "similarity": "cosine",
        }


class FakeIndices:
    def __init__(self) -> None:
        self.created = False

    async def exists(self, *, index: str) -> bool:
        return self.created

    async def create(self, **kwargs) -> None:
        self.created = True

    async def get_mapping(self, *, index: str) -> dict:
        raise AssertionError("新索引不应读取 mapping")


class FakeCluster:
    async def health(self, **kwargs) -> dict:
        return {"status": "green", "timed_out": False}


class FakeElasticsearchClient:
    def __init__(self) -> None:
        self.indices = FakeIndices()
        self.cluster = FakeCluster()
        self.indexed: dict | None = None

    async def index(self, **kwargs) -> None:
        self.indexed = kwargs

    async def get(self, **kwargs) -> dict:
        assert self.indexed is not None
        return {"found": True, "_source": self.indexed["document"]}

    async def close(self) -> None:
        return None


def test_elasticsearch_repository_uses_async_client_boundary() -> None:
    client = FakeElasticsearchClient()
    vector_store = Elasticsearch9ContractVectorStore(
        client=client,
        index_name="contracts",
        dimensions=2,
    )
    repository = ElasticsearchContractIndexRepository(vector_store)

    document = {
        "document_id": DOCUMENT_ID,
        "abstract": {"text": "摘要"},
        "document_visual_vector": [0.0, 1.0],
    }

    asyncio.run(repository.ensure_ready())
    asyncio.run(repository.save(document))

    assert client.indices.created is True
    assert client.indexed is not None
    assert client.indexed["id"] == DOCUMENT_ID
    assert asyncio.run(repository.get(DOCUMENT_ID)) == document
