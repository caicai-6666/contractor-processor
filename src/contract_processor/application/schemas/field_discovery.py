"""字段发现模式的应用层输入输出协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    attribute_result: tuple[dict[str, Any], ...]


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
    attribute_catalog_mode: Literal["empty_catalog", "active_catalog"]


class FieldDiscoveryBatchProcessingMetadata(StrictSchema):
    """绑定一次正式批次所使用的字段目录、模型、Prompt 与向量空间。"""

    model: str = Field(min_length=1)
    prompt_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str = Field(min_length=1)
    embedding_dimensions: int = Field(gt=0)
    embedding_instruction_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_schema_version: str = Field(min_length=1)
    attribute_schema_version: str = Field(min_length=1)
    core_catalog_mode: Literal["empty_catalog", "active_catalog"]
    attribute_catalog_mode: Literal["empty_catalog", "active_catalog"]
    max_candidates_per_document: int = Field(ge=1, le=5)
    top_k: int = Field(ge=1, le=5)


class FieldDiscoveryResult(StrictSchema):
    """发现模式专用结果，不携带 Clause 或 Abstract 占位字段。"""

    mode: Literal["discovery"]
    document_id: str = Field(pattern=SHA256_DOCUMENT_ID_PATTERN)
    source_name: str = Field(min_length=1)
    core: dict[str, Any]
    candidates: list[dict[str, Any]]
    discovery_metrics: dict[str, Any] = Field(default_factory=dict)
    processing: FieldDiscoveryProcessingMetadata


class FieldDiscoveryStageOneResult(StrictSchema):
    """批次第一阶段的候选身份、关系图、收敛结果与冻结字段。"""

    status: Literal["completed", "completed_with_failures"]
    document_count: int = Field(ge=0)
    succeeded_document_count: int = Field(ge=0)
    failed_document_count: int = Field(ge=0)
    raw_candidate_count: int = Field(ge=0)
    candidate_identity_count: int = Field(ge=0)
    source_group_count: int = Field(ge=0)
    succeeded_group_count: int = Field(ge=0)
    partially_succeeded_group_count: int = Field(ge=0)
    failed_group_count: int = Field(ge=0)
    final_field_count: int = Field(ge=0)
    discarded_candidate_count: int = Field(ge=0)
    batch_field_id_gate: Literal["passed", "failed"]
    batch_semantic_gate: Literal["passed", "failed"]
    candidate_count: int = Field(ge=0)
    documents: list[FieldDiscoveryResult]
    failed_documents: list[dict[str, str]]
    candidate_pool: list[dict[str, Any]]
    relation_graph: dict[str, Any]
    group_refinements: list[dict[str, Any]]
    global_semantic_gate: dict[str, Any]
    frozen_candidates: list[dict[str, Any]]


class FieldDiscoveryStageTwoResult(StrictSchema):
    """第二阶段逐字段回扫与确定性频率统计结果。"""

    status: Literal["completed", "completed_with_failures"]
    received_candidate_count: int = Field(ge=0)
    document_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    succeeded_task_count: int = Field(ge=0)
    failed_task_count: int = Field(ge=0)
    observations: list["CandidateFieldObservation"]
    statistics: list["CandidateFieldStatistics"]


class CandidateFieldObservation(StrictSchema):
    """一个冻结候选字段在一份合同上的独立抽取结论。"""

    task_ref: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    field_name: str = Field(min_length=1)
    document_id: str = Field(pattern=SHA256_DOCUMENT_ID_PATTERN)
    source_name: str = Field(min_length=1)
    task_status: Literal["succeeded", "failed"]
    extraction: dict[str, Any] | None = None
    attempt_count: int = Field(ge=0)
    error_type: str | None = None
    error: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_task_envelope(self) -> "CandidateFieldObservation":
        if self.task_status == "succeeded":
            if self.extraction is None:
                raise ValueError("成功的候选字段任务必须包含 extraction。")
            if self.error_type is not None or self.error is not None:
                raise ValueError("成功的候选字段任务不能包含错误信息。")
            if self.extraction.get("status") not in {
                "found",
                "not_found",
                "ambiguous",
                "conflicting",
                "not_applicable",
            }:
                raise ValueError("extraction 必须包含合法字段状态。")
        else:
            if self.extraction is not None:
                raise ValueError("失败的候选字段任务不能伪造 extraction。")
            if not self.error_type or not self.error:
                raise ValueError("失败的候选字段任务必须包含错误类型和错误信息。")
        return self


class CandidateFieldStatistics(StrictSchema):
    """按候选身份聚合的不同合同命中频率。"""

    candidate_ref: str = Field(min_length=1)
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    field_name: str = Field(min_length=1)
    document_count: int = Field(ge=0)
    scanned_document_count: int = Field(ge=0)
    found_document_count: int = Field(ge=0)
    not_found_document_count: int = Field(ge=0)
    ambiguous_document_count: int = Field(ge=0)
    conflicting_document_count: int = Field(ge=0)
    not_applicable_document_count: int = Field(ge=0)
    failed_document_count: int = Field(ge=0)
    frequency: float = Field(ge=0, le=1)
    conservative_frequency: float = Field(ge=0, le=1)
    found_document_ids: list[str]
    failed_document_ids: list[str]


class FieldDiscoveryBatchResult(StrictSchema):
    """批次 discovery 的两阶段结果协议。"""

    mode: Literal["discovery"]
    batch_id: str = Field(pattern=r"^discovery-[0-9]{8}T[0-9]{12}Z$")
    started_at: datetime
    completed_at: datetime
    processing: FieldDiscoveryBatchProcessingMetadata
    stage_one: FieldDiscoveryStageOneResult
    stage_two: FieldDiscoveryStageTwoResult

    @model_validator(mode="after")
    def _validate_batch_timeline(self) -> "FieldDiscoveryBatchResult":
        if self.completed_at < self.started_at:
            raise ValueError("字段发现批次 completed_at 不能早于 started_at。")
        return self
