"""专家终审合同入库的框架无关协议模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from contract_processor.application.schemas.contract_processing import (
    ContractProcessingResult,
)
from contract_processor.domain.identifiers import SHA256_DOCUMENT_ID_PATTERN


class IngestionSchema(BaseModel):
    """入库边界拒绝未约定字段，避免前后端协议静默漂移。"""

    model_config = ConfigDict(extra="forbid")


class ContractReviewTrace(IngestionSchema):
    """专家终审的最小追溯信息。"""

    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    comment: str = Field(min_length=1, max_length=4000)


class ContractReviewConfirmation(IngestionSchema):
    """接口层、应用层和实验共同使用的完整终审包络。"""

    document_id: str = Field(pattern=SHA256_DOCUMENT_ID_PATTERN)
    review: ContractReviewTrace
    result: ContractProcessingResult

    @model_validator(mode="after")
    def validate_confirmation(self) -> "ContractReviewConfirmation":
        self.validate_identity()
        return self

    def validate_identity(self) -> None:
        if self.document_id != self.result.document_id:
            raise ValueError("确认请求 document_id 与结果正文不一致。")


@dataclass(frozen=True, slots=True)
class CleaningMetrics:
    """稀疏投影清理的字段计数，不复制大段模型理由。"""

    core_before: int
    core_after: int
    attribute_before: int
    attribute_after: int
    removed_core_fields: tuple[str, ...]
    removed_attribute_fields: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "core_before": self.core_before,
            "core_after": self.core_after,
            "attribute_before": self.attribute_before,
            "attribute_after": self.attribute_after,
            "removed_core_fields": list(self.removed_core_fields),
            "removed_attribute_fields": list(self.removed_attribute_fields),
        }


@dataclass(frozen=True, slots=True)
class ContractSearchProjection:
    """完整终审对象面向 Elasticsearch 的稀疏业务投影。"""

    document: dict[str, Any]
    contract_name: str | None
    counterparty_names: tuple[str, ...]
    product_names: tuple[str, ...]
    abstract_text: str
    counterparty_resolution_status: str
    metrics: CleaningMetrics

    @property
    def text_embedding_inputs(self) -> dict[str, str]:
        inputs: dict[str, str] = {}
        if self.contract_name:
            inputs["contract_name_vector"] = self.contract_name
        if self.product_names:
            inputs["product_names_vector"] = "；".join(self.product_names)
        if self.abstract_text:
            inputs["abstract_vector"] = self.abstract_text
        return inputs


@dataclass(frozen=True, slots=True)
class StoredSourceDocument:
    """PDF 存储回执；storage_key 是相对键，不暴露服务器绝对路径。"""

    document_id: str
    storage_key: str
    mime_type: str
    size_bytes: int
    created: bool

    def as_metadata(self) -> dict[str, Any]:
        return {
            "storage_key": self.storage_key,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ContractIngestionOutcome:
    """独立入库子图完成后返回给未来接口层的结果。"""

    document_id: str
    source_name: str
    source_document: StoredSourceDocument
    cleaning: CleaningMetrics
    vector_fields: tuple[str, ...]
    vector_dimensions: int
    visual_page_count: int
    visual_strategy: str
    counterparty_resolution_status: str
    ingested_at: datetime
    index_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_name": self.source_name,
            "source_document": {
                **self.source_document.as_metadata(),
                "created": self.source_document.created,
            },
            "cleaning": self.cleaning.as_dict(),
            "vector_fields": list(self.vector_fields),
            "vector_dimensions": self.vector_dimensions,
            "visual_page_count": self.visual_page_count,
            "visual_strategy": self.visual_strategy,
            "counterparty_resolution_status": self.counterparty_resolution_status,
            "ingested_at": self.ingested_at.isoformat(),
            "index_name": self.index_name,
        }
