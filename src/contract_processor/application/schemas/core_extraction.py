"""从 Core YAML 动态生成结构化提取 JSON Schema。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


STATUS_VALUES = ["found", "not_found", "ambiguous", "conflicting", "not_applicable"]
PROPERTY_STATUS_VALUES = [*STATUS_VALUES, "out_of_scope"]


def _description(definition: dict[str, Any]) -> str:
    """将子字段业务规则组合为 JSON Schema description，供约束生成时直接注入。"""
    parts: list[str] = []
    for key, label in (
        ("name", "名称"),
        ("meaning", "含义"),
        ("format", "格式"),
        ("unit", "单位"),
        ("extraction_rule", "规则"),
    ):
        value = definition.get(key)
        if value not in (None, "", []):
            parts.append(f"{label}：{value}")
    not_meaning = definition.get("not_meaning", [])
    if not_meaning:
        parts.append(f"不包括：{'、'.join(str(item) for item in not_meaning)}")
    values = definition.get("values")
    if isinstance(values, dict):
        rendered_values = "；".join(f"{key}={value}" for key, value in values.items())
        parts.append(f"枚举：{rendered_values}")
    return "；".join(parts)


def output_definition_to_json_schema(definition: dict[str, Any]) -> dict[str, Any]:
    """递归转换字段输出定义；YAML 是类型、枚举和子字段语义的唯一来源。"""
    if "nullable" not in definition:
        raise ValueError("每个输出及子字段都必须显式声明 nullable")
    output_type = definition["type"]
    schema: dict[str, Any]
    if output_type == "object":
        properties = definition.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise ValueError("object 输出必须声明非空 properties")
        required = definition.get("required")
        if not isinstance(required, list):
            raise ValueError("object 输出必须显式声明 required")
        unknown_required = sorted(set(required) - set(properties))
        if unknown_required:
            raise ValueError(f"required 包含未定义子字段：{unknown_required}")
        missing_required = sorted(set(properties) - set(required))
        if missing_required:
            raise ValueError(f"properties 子字段必须全部列入 required：{missing_required}")
        schema = {
            "type": "object",
            "properties": {
                name: output_definition_to_json_schema(child)
                for name, child in properties.items()
            },
            "required": required,
            "additionalProperties": definition.get("additional_properties", False),
        }
    elif output_type == "array":
        items = definition.get("items")
        if not isinstance(items, dict):
            raise ValueError("array 输出必须声明 items")
        schema = {
            "type": "array",
            "items": output_definition_to_json_schema(items),
        }
        if "min_items" in definition:
            schema["minItems"] = definition["min_items"]
        if "max_items" in definition:
            schema["maxItems"] = definition["max_items"]
    elif output_type == "enum":
        values = definition.get("values")
        if isinstance(values, dict):
            enum_values = list(values)
        elif isinstance(values, list):
            enum_values = values
        else:
            raise ValueError("enum 输出必须声明 values")
        schema = {"type": "string", "enum": enum_values}
    elif output_type == "date":
        schema = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    elif output_type in {"string", "number", "integer", "boolean"}:
        schema = {"type": output_type}
    else:
        raise ValueError(f"不支持的输出类型：{output_type}")

    description = _description(definition)
    if description:
        schema["description"] = description
    for source_key, schema_key in (
        ("minimum", "minimum"),
        ("maximum", "maximum"),
        ("min_length", "minLength"),
        ("max_length", "maxLength"),
        ("pattern", "pattern"),
    ):
        if source_key in definition:
            schema[schema_key] = definition[source_key]

    if definition.get("nullable", False):
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _field_envelope_schema(
    *, field_id: str, value_schema: dict[str, Any]
) -> dict[str, Any]:
    """按原文、理由、状态、规范值生成非对象字段包络。"""

    return {
        "type": "object",
        "properties": {
            "raw_value": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "用于追溯的最小必要原始值文本，不包含解释或推理",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "基于原文简述当前字段的采用、缺失、冲突或排除理由",
            },
            "status": {"type": "string", "enum": STATUS_VALUES},
            "value": deepcopy(value_schema),
        },
        "required": [
            "raw_value",
            "reason",
            "status",
            "value",
        ],
        "additionalProperties": False,
        "description": f"Core 字段 {field_id} 的统一值包络",
    }


def _nullable_envelope_value_schema(value_schema: dict[str, Any]) -> dict[str, Any]:
    """状态包络必须允许 null；found 时的非空约束由应用层结合 status 校验。"""

    any_of = value_schema.get("anyOf")
    if isinstance(any_of, list) and any(
        isinstance(item, dict) and item.get("type") == "null" for item in any_of
    ):
        return deepcopy(value_schema)
    return {"anyOf": [deepcopy(value_schema), {"type": "null"}]}


def _property_envelope_schema(
    *, property_name: str, value_schema: dict[str, Any]
) -> dict[str, Any]:
    """为对象直属子字段生成原文、理由、状态和值的独立决策包络。"""

    return {
        "type": "object",
        "properties": {
            "raw_value": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "当前子字段相关的最小必要合同原文；未发现或不适用时为 null",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "基于原文简述当前子字段的采用、缺失、冲突或排除理由",
            },
            "status": {"type": "string", "enum": PROPERTY_STATUS_VALUES},
            "value": _nullable_envelope_value_schema(value_schema),
        },
        "required": ["raw_value", "reason", "status", "value"],
        "additionalProperties": False,
        "description": f"对象直属子字段 {property_name} 的独立决策包络",
    }


def _object_field_schema(
    *, field_id: str, output_definition: dict[str, Any]
) -> dict[str, Any]:
    """对象字段只让模型生成直属子字段；对象总状态由应用层确定性汇总。"""

    properties = output_definition.get("properties")
    required = output_definition.get("required")
    if not isinstance(properties, dict) or not properties:
        raise ValueError(f"对象字段 {field_id} 必须声明非空 properties")
    if not isinstance(required, list) or set(required) != set(properties):
        raise ValueError(f"对象字段 {field_id} 的所有 properties 都必须列入 required")
    property_envelopes = {
        name: _property_envelope_schema(
            property_name=name,
            value_schema=output_definition_to_json_schema(child),
        )
        for name, child in properties.items()
    }
    return {
        "type": "object",
        "properties": {
            "properties": {
                "type": "object",
                "properties": property_envelopes,
                "required": required,
                "additionalProperties": False,
            }
        },
        "required": ["properties"],
        "additionalProperties": False,
        "description": (
            f"Core 对象字段 {field_id} 的直属子字段结果；不得输出对象外层 status，"
            "该状态由应用层根据子字段状态汇总"
        ),
    }


def build_core_extraction_schema(core_fields: list[dict[str, Any]]) -> dict[str, Any]:
    """生成禁止未知键的 Schema；对象字段细化到直属子字段决策包络。"""
    field_properties: dict[str, Any] = {}
    for field in core_fields:
        field_id = field["field_id"]
        if field_id in field_properties:
            raise ValueError(f"Core field_id 重复：{field_id}")
        output = field["output"]
        if output["type"] == "object":
            field_properties[field_id] = _object_field_schema(
                field_id=field_id,
                output_definition=output,
            )
        else:
            field_properties[field_id] = _field_envelope_schema(
                field_id=field_id,
                value_schema=output_definition_to_json_schema(output),
            )
    return {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "minLength": 1},
            "fields": {
                "type": "object",
                "properties": field_properties,
                "required": list(field_properties),
                "additionalProperties": False,
            },
        },
        "required": ["document_id", "fields"],
        "additionalProperties": False,
    }
