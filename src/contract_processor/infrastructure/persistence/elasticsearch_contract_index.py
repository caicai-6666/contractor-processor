"""Elasticsearch 9 合同多向量索引与 LlamaIndex 适配器。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol, Sequence

from elasticsearch import NotFoundError
from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.vector_stores.types import (
    VectorStoreQuery,
    VectorStoreQueryResult,
)


VECTOR_FIELDS = (
    "contract_name_vector",
    "product_names_vector",
    "abstract_vector",
    "document_visual_vector",
)
CHINESE_TEXT_ANALYZER = "smartcn"
CHINESE_TEXT_FIELDS = (
    "source_name",
    "clause.heading",
    "clause.source_text",
    "abstract.text",
    "contract_name",
    "counterparty_names",
    "product_names",
)
DOCUMENT_METADATA_KEY = "_contract_elasticsearch_document"


class ElasticsearchClient(Protocol):
    class Indices(Protocol):
        async def exists(self, *, index: str) -> bool: ...

        async def create(self, **kwargs: Any) -> Any: ...

        async def get_mapping(self, *, index: str) -> dict[str, Any]: ...

    class Cluster(Protocol):
        async def health(self, **kwargs: Any) -> dict[str, Any]: ...

    indices: Indices
    cluster: Cluster

    async def index(self, **kwargs: Any) -> Any: ...

    async def get(self, **kwargs: Any) -> Any: ...

    async def delete(self, **kwargs: Any) -> Any: ...

    async def search(self, **kwargs: Any) -> Any: ...


def _smartcn_text_mapping(*, keyword_ignore_above: int | None = None) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "type": "text",
        "analyzer": CHINESE_TEXT_ANALYZER,
        "search_analyzer": CHINESE_TEXT_ANALYZER,
    }
    if keyword_ignore_above is not None:
        mapping["fields"] = {
            "keyword": {"type": "keyword", "ignore_above": keyword_ignore_above}
        }
    return mapping


def _field_mapping(mappings: dict[str, Any], dotted_path: str) -> dict[str, Any]:
    current = mappings
    for part in dotted_path.split("."):
        value = current.get("properties", {}).get(part)
        if not isinstance(value, dict):
            return {}
        current = value
    return current


def build_contract_index_mapping(dimensions: int) -> dict[str, Any]:
    """构建正式单文档四向量 strict mapping。"""

    if dimensions < 1:
        raise ValueError("向量维度必须大于 0。")
    vector_mapping = {
        "type": "dense_vector",
        "dims": dimensions,
        "index": True,
        "similarity": "cosine",
    }
    return {
        "dynamic": "strict",
        "properties": {
            "document_id": {"type": "keyword"},
            "source_name": _smartcn_text_mapping(keyword_ignore_above=1024),
            "source_document": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "storage_key": {"type": "keyword", "ignore_above": 1024},
                    "mime_type": {"type": "keyword"},
                    "size_bytes": {"type": "long"},
                },
            },
            "review": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "reviewer": {"type": "keyword", "ignore_above": 200},
                    "reviewed_at": {"type": "date"},
                    "comment": {"type": "text", "index": False},
                },
            },
            # 完整终审包络保留在 _source；动态字段不参与 mapping 推断和检索。
            "reviewed_result": {"type": "object", "enabled": False},
            "core_values": {"type": "flattened"},
            "attribute_values": {"type": "flattened"},
            "clause": {
                "type": "nested",
                "dynamic": "strict",
                "properties": {
                    "clause_number": {"type": "keyword", "ignore_above": 128},
                    "heading": _smartcn_text_mapping(keyword_ignore_above=512),
                    "category": {"type": "keyword"},
                    "source_text": _smartcn_text_mapping(),
                    "page_refs": {"type": "integer"},
                },
            },
            "abstract": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "sections": {"type": "object", "enabled": False},
                    "text": _smartcn_text_mapping(),
                },
            },
            "processing": {"type": "flattened"},
            "contract_name": _smartcn_text_mapping(keyword_ignore_above=1024),
            "counterparty_names": _smartcn_text_mapping(keyword_ignore_above=1024),
            "product_names": _smartcn_text_mapping(keyword_ignore_above=1024),
            "counterparty_resolution_status": {"type": "keyword"},
            "vectorization": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "model": {"type": "keyword"},
                    "instruction_version": {"type": "keyword"},
                    "dimensions": {"type": "integer"},
                    "normalized": {"type": "boolean"},
                    "visual_strategy": {"type": "keyword"},
                    "visual_page_count": {"type": "integer"},
                    "embedded_fields": {"type": "keyword"},
                },
            },
            "ingestion_attempt_id": {"type": "keyword"},
            "ingested_at": {"type": "date"},
            **{field: deepcopy(vector_mapping) for field in VECTOR_FIELDS},
        },
    }


def find_mapping_incompatibilities(
    mappings: dict[str, Any], dimensions: int
) -> tuple[str, ...]:
    """在昂贵向量化之前拒绝不兼容正式索引。"""

    properties = mappings.get("properties", {})
    incompatible: list[str] = []
    if mappings.get("dynamic") != "strict":
        incompatible.append("dynamic 必须为 strict")
    expected = build_contract_index_mapping(dimensions)
    missing = sorted(set(expected["properties"]) - set(properties))
    if missing:
        incompatible.append(f"缺少字段：{missing}")
    for field in CHINESE_TEXT_FIELDS:
        actual = _field_mapping(mappings, field)
        analyzer = actual.get("analyzer")
        search_analyzer = actual.get("search_analyzer", analyzer)
        if analyzer != CHINESE_TEXT_ANALYZER:
            incompatible.append(
                f"{field}.analyzer={analyzer!r}，要求 {CHINESE_TEXT_ANALYZER!r}"
            )
        if search_analyzer != CHINESE_TEXT_ANALYZER:
            incompatible.append(
                f"{field}.search_analyzer={search_analyzer!r}，"
                f"要求 {CHINESE_TEXT_ANALYZER!r}"
            )
    for field in VECTOR_FIELDS:
        actual = properties.get(field, {})
        if actual.get("type") != "dense_vector":
            incompatible.append(f"{field}.type 不是 dense_vector")
        if actual.get("dims") != dimensions:
            incompatible.append(f"{field}.dims={actual.get('dims')}，要求 {dimensions}")
        if actual.get("similarity") != "cosine":
            incompatible.append(f"{field}.similarity 不是 cosine")
    reviewed_result = properties.get("reviewed_result", {})
    if reviewed_result.get("enabled") is not False:
        incompatible.append("reviewed_result.enabled 必须为 false")
    for field, expected_type in {
        "source_document.storage_key": "keyword",
        "source_document.mime_type": "keyword",
        "source_document.size_bytes": "long",
        "vectorization.instruction_version": "keyword",
        "ingestion_attempt_id": "keyword",
    }.items():
        actual_type = _field_mapping(mappings, field).get("type")
        if actual_type != expected_type:
            incompatible.append(
                f"{field}.type={actual_type!r}，要求 {expected_type!r}"
            )
    return tuple(dict.fromkeys(incompatible))


class ElasticsearchMappingFactory:
    """保留统一异步 mapping 构建入口。"""

    def __init__(self, core_catalog_path: Path | None = None) -> None:
        # 参数为旧调用兼容而保留；正式检索只索引清洗后的值投影，完整动态包络关闭解析。
        self._core_catalog_path = core_catalog_path

    async def build(self, *, vector_dimensions: int | None = None) -> dict[str, Any]:
        if vector_dimensions is None:
            raise ValueError("正式合同索引必须配置 vector_dimensions。")
        return build_contract_index_mapping(vector_dimensions)


def build_contract_node(document: dict[str, Any]) -> TextNode:
    """将一份多向量合同封装为 LlamaIndex 节点。"""

    document_id = document.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("ES 文档缺少 document_id。")
    abstract = document.get("abstract")
    text = abstract.get("text", "") if isinstance(abstract, dict) else ""
    embedding = document.get("abstract_vector") or document.get(
        "document_visual_vector"
    )
    if not isinstance(embedding, list):
        raise ValueError("合同节点至少需要 Abstract 或视觉向量。")
    return TextNode(
        id_=document_id,
        text=text,
        embedding=embedding,
        metadata={DOCUMENT_METADATA_KEY: deepcopy(document)},
        excluded_embed_metadata_keys=[DOCUMENT_METADATA_KEY],
        excluded_llm_metadata_keys=[DOCUMENT_METADATA_KEY],
    )


class Elasticsearch9ContractVectorStore:
    """LlamaIndex VectorStore 协议适配器：一份合同对应一条 ES 文档。"""

    stores_text = True
    is_embedding_query = True

    def __init__(
        self,
        *,
        client: ElasticsearchClient,
        index_name: str,
        dimensions: int,
        refresh: str = "wait_for",
        number_of_shards: int = 1,
        number_of_replicas: int = 0,
    ) -> None:
        self._client = client
        self._index_name = index_name
        self._dimensions = dimensions
        self._mapping = build_contract_index_mapping(dimensions)
        self._refresh = refresh
        self._number_of_shards = number_of_shards
        self._number_of_replicas = number_of_replicas
        self._ready = False
        self._ready_lock = asyncio.Lock()

    @property
    def client(self) -> ElasticsearchClient:
        return self._client

    @property
    def index_name(self) -> str:
        return self._index_name

    async def ensure_index(self) -> str:
        if self._ready:
            return "validated"
        async with self._ready_lock:
            if self._ready:
                return "validated"
            if not await self._client.indices.exists(index=self._index_name):
                await self._client.indices.create(
                    index=self._index_name,
                    mappings=self._mapping,
                    settings={
                        "number_of_shards": self._number_of_shards,
                        "number_of_replicas": self._number_of_replicas,
                    },
                    # ES 9 使用数值 0 表示不等待分片，随后显式检查主分片健康。
                    wait_for_active_shards=0,
                )
                status = "created"
                mappings = self._mapping
            else:
                response = await self._client.indices.get_mapping(index=self._index_name)
                mappings = dict(response)[self._index_name].get("mappings", {})
                status = "validated"
            incompatible = find_mapping_incompatibilities(mappings, self._dimensions)
            if incompatible:
                raise RuntimeError(
                    f"索引 {self._index_name} mapping 不兼容："
                    f"{'；'.join(incompatible)}。程序不会隐式迁移或重建。"
                )
            health = await self._client.cluster.health(index=self._index_name)
            if health.get("timed_out") or health.get("status") == "red":
                raise RuntimeError(
                    f"索引 {self._index_name} 主分片不可用"
                    f"（status={health.get('status')}）。"
                )
            self._ready = True
            return status

    def add(self, nodes: Sequence[BaseNode], **kwargs: Any) -> list[str]:
        raise RuntimeError("该 VectorStore 只支持 async_add。")

    async def async_add(
        self, nodes: Sequence[BaseNode], **kwargs: Any
    ) -> list[str]:
        await self.ensure_index()
        ids: list[str] = []
        for node in nodes:
            document = node.metadata.get(DOCUMENT_METADATA_KEY)
            if not isinstance(document, dict):
                raise ValueError(
                    f"LlamaIndex 节点缺少 {DOCUMENT_METADATA_KEY} 元数据。"
                )
            if document.get("document_id") != node.node_id:
                raise ValueError("LlamaIndex node_id 与 ES document_id 不一致。")
            for field in VECTOR_FIELDS:
                vector = document.get(field)
                if vector is not None and (
                    not isinstance(vector, list) or len(vector) != self._dimensions
                ):
                    raise ValueError(f"{field} 向量维度不符合 mapping。")
            await self._client.index(
                index=self._index_name,
                id=node.node_id,
                document=document,
                refresh=self._refresh,
            )
            ids.append(node.node_id)
        return ids

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        raise RuntimeError("该 VectorStore 只支持 adelete。")

    async def adelete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        await self._client.delete(
            index=self._index_name,
            id=ref_doc_id,
            refresh=self._refresh,
        )

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        raise RuntimeError("该 VectorStore 只支持 aquery。")

    async def aquery(
        self, query: VectorStoreQuery, **kwargs: Any
    ) -> VectorStoreQueryResult:
        vector = query.query_embedding
        if vector is None:
            raise ValueError("KNN 查询缺少 query_embedding。")
        if len(vector) != self._dimensions:
            raise ValueError("KNN 查询向量维度不符合 mapping。")
        field = query.embedding_field or "abstract_vector"
        if field not in VECTOR_FIELDS:
            raise ValueError(f"不支持的向量字段：{field}")
        top_k = max(1, query.similarity_top_k)
        response = await self._client.search(
            index=self._index_name,
            knn={
                "field": field,
                "query_vector": vector,
                "k": top_k,
                "num_candidates": max(top_k * 10, 10),
            },
            size=top_k,
            source_excludes=list(VECTOR_FIELDS),
        )
        nodes: list[TextNode] = []
        ids: list[str] = []
        similarities: list[float] = []
        for hit in response["hits"]["hits"]:
            source = hit.get("_source", {})
            abstract = source.get("abstract", {})
            text = abstract.get("text", "") if isinstance(abstract, dict) else ""
            node_id = str(hit["_id"])
            nodes.append(TextNode(id_=node_id, text=text, metadata=source))
            ids.append(node_id)
            similarities.append(float(hit.get("_score") or 0.0))
        return VectorStoreQueryResult(
            nodes=nodes,
            similarities=similarities,
            ids=ids,
        )


class ElasticsearchContractIndexRepository:
    """正式仓储：经 LlamaIndex 写入，并按 document_id 回读校验。"""

    def __init__(self, vector_store: Elasticsearch9ContractVectorStore) -> None:
        self._vector_store = vector_store

    @property
    def index_name(self) -> str:
        return self._vector_store.index_name

    @property
    def vector_store(self) -> Elasticsearch9ContractVectorStore:
        return self._vector_store

    async def ensure_ready(self) -> str:
        return await self._vector_store.ensure_index()

    async def save(self, document: dict[str, Any]) -> None:
        node = build_contract_node(document)
        ids = await self._vector_store.async_add([node])
        if ids != [document["document_id"]]:
            raise RuntimeError(f"VectorStore 返回异常文档 ID：{ids}")

    async def get(self, document_id: str) -> dict[str, Any] | None:
        try:
            response = await self._vector_store.client.get(
                index=self.index_name,
                id=document_id,
                source_excludes=list(VECTOR_FIELDS),
            )
        except NotFoundError:
            return None
        if not response.get("found", True):
            return None
        source = response.get("_source")
        return source if isinstance(source, dict) else None

    async def close(self) -> None:
        close = getattr(self._vector_store.client, "close", None)
        if close is not None:
            await close()
