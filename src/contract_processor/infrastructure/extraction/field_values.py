"""Core 与固定 Attribute 共用的字段值包络和确定性校验。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictFieldModel(BaseModel):
    """字段抽取模型响应禁止未约定键，保证目录驱动的协议稳定。"""

    model_config = ConfigDict(extra="forbid")


FieldStatus = Literal[
    "found", "not_found", "ambiguous", "conflicting", "not_applicable"
]
PropertyStatus = Literal[
    "found",
    "not_found",
    "ambiguous",
    "conflicting",
    "not_applicable",
    "out_of_scope",
]


class ScalarFieldValue(StrictFieldModel):
    """标量、数组等非对象字段的原文、判断和规范值包络。"""

    raw_value: str | None = Field(
        default=None, description="用于追溯的原始值；不得用模型总结代替原文"
    )
    reason: str = Field(min_length=1, max_length=400)
    status: FieldStatus
    value: Any | None = Field(default=None, description="按字段定义规范化后的值")


class ObjectPropertyValue(StrictFieldModel):
    """对象直属子字段的独立决策包络。"""

    raw_value: str | None = Field(default=None, description="当前子字段相关的最小必要原文")
    reason: str = Field(min_length=1, max_length=400)
    status: PropertyStatus
    value: Any | None = Field(default=None, description="当前子字段的规范值")


class ObjectFieldCandidate(StrictFieldModel):
    """模型只生成对象子字段；外层状态由程序确定性汇总。"""

    properties: dict[str, ObjectPropertyValue]


class ObjectFieldValue(StrictFieldModel):
    """最终对象字段，包含由直属子字段汇总的外层状态。"""

    status: FieldStatus
    properties: dict[str, ObjectPropertyValue]


class FieldExtractionCandidate(StrictFieldModel):
    """一次逐字段调用的统一结构化响应。"""

    fields: dict[str, ScalarFieldValue | ObjectFieldCandidate]


FinalFieldValue = ScalarFieldValue | ObjectFieldValue
CandidateFieldValue = ScalarFieldValue | ObjectFieldCandidate


def validate_scalar_field_envelope(
    _field_id: str, field: ScalarFieldValue
) -> list[str]:
    """校验 JSON Schema 无法表达的 status/value 关系。"""

    errors: list[str] = []
    empty_statuses = {"not_found", "ambiguous", "conflicting", "not_applicable"}
    if field.status == "found" and field.value is None:
        errors.append("found 状态必须包含非 null value")
    if field.status in empty_statuses and field.value is not None:
        errors.append(f"{field.status} 状态的 value 必须为 null")
    return errors


def validate_property_envelope(
    property_name: str, field: ObjectPropertyValue
) -> list[str]:
    """校验对象子字段状态，并保留冲突或排除时的原文追溯。"""

    errors: list[str] = []
    if field.status == "found" and field.value is None:
        errors.append("found 状态必须包含非 null value")
    if field.status in {"not_found", "not_applicable"} and field.value is not None:
        errors.append(f"{field.status} 状态的 value 必须为 null")
    if field.status in {"ambiguous", "conflicting", "out_of_scope"}:
        if field.value is not None:
            errors.append(f"{field.status} 状态的 value 必须为 null")
        if field.raw_value is None:
            errors.append(f"{field.status} 状态必须保留相关 raw_value")
    return [f"{property_name}: {error}" for error in errors]


def aggregate_object_status(
    properties: dict[str, ObjectPropertyValue],
) -> FieldStatus:
    """局部空值不覆盖已找到的对象子字段，状态汇总保持确定性。"""

    statuses = {property_value.status for property_value in properties.values()}
    if "found" in statuses:
        return "found"
    if "conflicting" in statuses:
        return "conflicting"
    if "ambiguous" in statuses:
        return "ambiguous"
    if statuses == {"not_applicable"}:
        return "not_applicable"
    return "not_found"


def validate_extracted_field(
    field_id: str,
    field: ScalarFieldValue | ObjectFieldCandidate | ObjectFieldValue,
) -> list[str]:
    """统一校验标量字段与对象直属子字段包络。"""

    if isinstance(field, ScalarFieldValue):
        return validate_scalar_field_envelope(field_id, field)
    errors = [
        error
        for property_name, property_value in field.properties.items()
        for error in validate_property_envelope(property_name, property_value)
    ]
    if isinstance(field, ObjectFieldValue):
        expected_status = aggregate_object_status(field.properties)
        if field.status != expected_status:
            errors.append(
                f"对象外层 status 应由子字段汇总为 {expected_status}，实际为 {field.status}"
            )
    return errors


def finalize_candidate_field(
    field: CandidateFieldValue,
) -> FinalFieldValue:
    """把模型对象候选转为包含确定性外层状态的最终字段。"""

    if isinstance(field, ScalarFieldValue):
        return field
    return ObjectFieldValue(
        status=aggregate_object_status(field.properties),
        properties=field.properties,
    )


def aggregate_field_metrics(field_records: list[dict[str, Any]]) -> dict[str, Any]:
    """保留逐字段指标，同时提供阶段级汇总以便定位局部失败。"""

    successful_count = sum(record["status"] == "succeeded" for record in field_records)
    failed_count = sum(record["status"] == "failed" for record in field_records)
    aggregate_usage = {
        key: sum(
            int(record.get("metrics", {}).get("usage", {}).get(key) or 0)
            for record in field_records
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return {
        "field_count": len(field_records),
        "successful_field_count": successful_count,
        "failed_field_count": failed_count,
        "aggregate_elapsed_seconds": round(
            sum(
                float(record.get("metrics", {}).get("elapsed_seconds") or 0)
                for record in field_records
            ),
            3,
        ),
        "aggregate_usage": aggregate_usage,
        "fields": field_records,
    }
