"""正式合同处理工作流的应用层输入输出模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contract_processor.domain.identifiers import SHA256_DOCUMENT_ID_PATTERN


class StrictSchema(BaseModel):
    """禁止协议对象悄悄接收前后端尚未约定的字段。"""

    model_config = ConfigDict(extra="forbid")


class ContractAbstract(StrictSchema):
    """合同级固定摘要；只有 ``text`` 参与后续向量化。"""

    sections: dict[str, Any]
    text: str


class AttributeExtractionDiagnostics(StrictSchema):
    """区分业务未命中与模型/结构校验失败，供专家复核和重试。"""

    status: Literal["completed", "completed_with_failures"] = "completed"
    validation: dict[str, Any] = Field(default_factory=dict)
    skipped_field_ids: list[str] = Field(default_factory=list)
    successful_field_count: int = Field(default=0, ge=0)
    failed_field_count: int = Field(default=0, ge=0)
    failed_fields: list[dict[str, Any]] = Field(default_factory=list)


class ProcessingMetadata(StrictSchema):
    """随候选返回的可复现版本信息，不包含本地运行目录。"""

    model: str
    prompt_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page_count: int = Field(ge=1)
    core_schema_version: str
    attribute_schema_version: str
    clause_schema_version: str
    summary_schema_version: str
    attribute_extraction: AttributeExtractionDiagnostics = Field(
        default_factory=AttributeExtractionDiagnostics
    )


class ContractProcessingResult(StrictSchema):
    """前端终审与 Elasticsearch 持久化共同使用的合同候选协议。"""

    document_id: str = Field(pattern=SHA256_DOCUMENT_ID_PATTERN)
    source_name: str = Field(min_length=1)
    core: dict[str, Any]
    attribute: list[dict[str, Any]]
    clause: list[dict[str, Any]]
    abstract: ContractAbstract
    processing: ProcessingMetadata
