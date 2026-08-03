"""兼容导出；实验不再维护第二套 Elasticsearch 实现。"""

from contract_processor.infrastructure.persistence.elasticsearch_contract_index import (
    CHINESE_TEXT_ANALYZER,
    CHINESE_TEXT_FIELDS,
    DOCUMENT_METADATA_KEY,
    VECTOR_FIELDS,
    Elasticsearch9ContractVectorStore,
    build_contract_index_mapping,
    build_contract_node,
    find_mapping_incompatibilities,
)

build_ingestion_mapping = build_contract_index_mapping

__all__ = [
    "CHINESE_TEXT_ANALYZER",
    "CHINESE_TEXT_FIELDS",
    "DOCUMENT_METADATA_KEY",
    "VECTOR_FIELDS",
    "Elasticsearch9ContractVectorStore",
    "build_contract_node",
    "build_ingestion_mapping",
    "find_mapping_incompatibilities",
]
