"""正式合同处理工作流的应用层输入输出模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract_processor.domain.identifiers import SHA256_DOCUMENT_ID_PATTERN


class StrictSchema(BaseModel):
    """禁止协议对象悄悄接收前后端尚未约定的字段。"""

    model_config = ConfigDict(extra="forbid")


class ContractAbstract(StrictSchema):
    """合同级固定摘要；只有 ``text`` 参与后续向量化。"""

    sections: dict[str, Any]
    text: str


class ProcessingMetadata(StrictSchema):
    """随候选返回的可复现版本信息，不包含本地运行目录。"""

    model: str
    prompt_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page_count: int = Field(ge=1)
    core_schema_version: str
    attribute_schema_version: str
    clause_schema_version: str
    summary_schema_version: str


class ContractProcessingResult(StrictSchema):
    """前端终审与 Elasticsearch 持久化共同使用的合同候选协议。"""

    document_id: str = Field(pattern=SHA256_DOCUMENT_ID_PATTERN)
    source_name: str = Field(min_length=1)
    core: dict[str, Any]
    attribute: list[dict[str, Any]]
    clause: list[dict[str, Any]]
    abstract: ContractAbstract
    processing: ProcessingMetadata
