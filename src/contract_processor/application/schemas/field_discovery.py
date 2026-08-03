"""字段发现模式的应用层输入输出协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contract_processor.domain.identifiers import SHA256_DOCUMENT_ID_PATTERN
from contract_processor.domain.models import FieldDefinition


@dataclass(frozen=True, slots=True)
class RenderedDocumentPage:
    """字段发现服务可读取、但不能修改的单页多模态输入。"""

    page_number: int
    data_url: str
    image_bytes: int


@dataclass(frozen=True, slots=True)
class FieldDiscoveryRequest:
    """发现端口所需的原文、已知定义和 Core 上下文。"""

    document_id: str
    contract_path: Path
    pages: tuple[RenderedDocumentPage, ...]
    core_definitions: tuple[FieldDefinition, ...]
    core_result: dict[str, Any]
    attribute_definitions: tuple[FieldDefinition, ...]


@dataclass(frozen=True, slots=True)
class FieldDiscoveryOutput:
    """具体发现算法的阶段输出；当前只冻结接入协议。"""

    candidates: tuple[dict[str, Any], ...]
    metrics: dict[str, Any] = field(default_factory=dict)


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldDiscoveryProcessingMetadata(StrictSchema):
    """支持复现发现运行的最小版本信息。"""

    model: str
    prompt_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page_count: int = Field(ge=1)
    core_schema_version: str
    attribute_schema_version: str
    core_catalog_mode: Literal["empty_catalog", "active_catalog"]


class FieldDiscoveryResult(StrictSchema):
    """发现模式专用结果，不携带 Clause 或 Abstract 占位字段。"""

    mode: Literal["discovery"]
    document_id: str = Field(pattern=SHA256_DOCUMENT_ID_PATTERN)
    source_name: str = Field(min_length=1)
    core: dict[str, Any]
    candidates: list[dict[str, Any]]
    processing: FieldDiscoveryProcessingMetadata
