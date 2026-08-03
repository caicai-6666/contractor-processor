"""合同元数据的框架无关数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from contract_processor.domain.enums import (
    ExtractionStatus,
    FieldKind,
    MergeAction,
    ReviewStatus,
)
from contract_processor.domain.identifiers import validate_document_id


@dataclass(frozen=True, slots=True)
class Evidence:
    """一个抽取结论可回溯的原始证据。"""

    page_number: int
    source_text: str
    value_path: str | None = None
    bounding_box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class Contract:
    """以原始 PDF 文件 SHA-256 作为稳定文档标识。"""

    document_id: str
    source_name: str

    def __post_init__(self) -> None:
        """在领域边界拒绝非 SHA-256 文档标识。"""

        validate_document_id(self.document_id)


@dataclass(frozen=True, slots=True)
class BatchRun:
    """一次固定合同批次处理的可追溯上下文。"""

    batch_id: str
    document_ids: tuple[str, ...]
    field_catalog_version: str
    model_version: str


@dataclass(frozen=True, slots=True)
class OutputDefinition:
    """可递归的字段值输出约束；复杂子字段不能退化为格式字符串。"""

    type: str
    format: str | None
    nullable: bool
    example: Any = None
    name: str | None = None
    meaning: str | None = None
    unit: str | None = None
    not_meaning: tuple[str, ...] = ()
    extraction_rule: str | None = None
    enum_values: tuple[str, ...] = ()
    enum_descriptions: tuple[tuple[str, str], ...] = ()
    properties: tuple[tuple[str, OutputDefinition], ...] = ()
    required: tuple[str, ...] = ()
    additional_properties: bool = False
    items: OutputDefinition | None = None
    minimum: float | None = None
    maximum: float | None = None
    pattern: str | None = None
    min_items: int | None = None
    max_items: int | None = None
    min_length: int | None = None
    max_length: int | None = None

    def __post_init__(self) -> None:
        """校验递归类型定义，阻止格式不完整的复杂字段进入字段目录。"""

        property_names = tuple(name for name, _ in self.properties)
        if len(property_names) != len(set(property_names)):
            raise ValueError("object properties 不得包含重复名称")
        if self.type == "object":
            if not self.properties:
                raise ValueError("object 输出必须定义 properties")
            if set(self.required) != set(property_names):
                raise ValueError("object 的所有 properties 都必须列入 required")
            if self.additional_properties:
                raise ValueError("Core/Attribute object 不允许额外子字段")
        if self.type == "array" and self.items is None:
            raise ValueError("array 输出必须定义 items")
        if self.type == "enum" and not self.enum_values:
            raise ValueError("enum 输出必须定义 values")

    def property(self, name: str) -> OutputDefinition:
        """按名称读取复杂对象子字段，未定义时明确失败。"""

        for property_name, definition in self.properties:
            if property_name == name:
                return definition
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class FieldExample:
    """用于抽取提示词的字段示例。"""

    source_text: str
    output: Any


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    """Core 与 Attribute 共用的字段定义。"""

    field_id: str
    name: str
    meaning: str
    aliases: tuple[str, ...]
    not_meaning: tuple[str, ...]
    output: OutputDefinition
    extraction_rule: str
    examples: tuple[FieldExample, ...]
    kind: FieldKind

    def summary(self) -> str:
        """构造用于向量召回的稳定字段摘要。"""

        aliases = "、".join(self.aliases)
        return f"名称：{self.name}\n含义：{self.meaning}\n别名：{aliases}"


@dataclass(frozen=True, slots=True)
class FieldCatalogSnapshot:
    """一次运行固定读取的字段目录快照。"""

    kind: FieldKind
    schema_version: str
    status: str
    definitions: tuple[FieldDefinition, ...]

    @property
    def is_empty(self) -> bool:
        """空目录必须由显式状态和零字段共同表达。"""

        return self.status == "empty" and not self.definitions

    @property
    def field_count(self) -> int:
        return len(self.definitions)


@dataclass(frozen=True, slots=True)
class FieldObservation:
    """模型在单份合同中观察到的精简字段包络。"""

    field_id: str | None
    name: str
    meaning: str
    raw_value: str | None
    status: ExtractionStatus
    value: Any
    document_id: str

    def __post_init__(self) -> None:
        """在领域边界阻止状态、规范值和原始值相互矛盾的结果进入工作流。"""

        validate_document_id(self.document_id)
        empty_statuses = {
            ExtractionStatus.NOT_FOUND,
            ExtractionStatus.AMBIGUOUS,
            ExtractionStatus.CONFLICTING,
            ExtractionStatus.NOT_APPLICABLE,
        }
        if self.status is ExtractionStatus.FOUND:
            if self.value is None:
                raise ValueError("found 状态必须包含非空 value")
        else:
            if self.value is not None:
                raise ValueError(f"{self.status.value} 状态的 value 必须为空")
        # raw_value 承担审计职责；即使没有可采用 value，仍可保留最小相关原文。


@dataclass(frozen=True, slots=True)
class AttributeStatistics:
    """Attribute 的发现频次；合同数是专家审核的主排序指标。"""

    occurrence_count: int = 0
    document_ids: frozenset[str] = frozenset()
    first_seen_round: str | None = None
    last_seen_round: str | None = None

    @property
    def contract_count(self) -> int:
        return len(self.document_ids)


@dataclass(frozen=True, slots=True)
class AttributeRecord:
    """动态字段定义及其审核、统计信息。"""

    definition: FieldDefinition
    statistics: AttributeStatistics = field(default_factory=AttributeStatistics)
    review_status: ReviewStatus = ReviewStatus.PENDING
    decision_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MergeDecision:
    """候选字段与字段库比对后的、可审计的归并结论。"""

    action: MergeAction
    candidate_name: str
    target_field_id: str | None
    reason: str
    decided_at: datetime
