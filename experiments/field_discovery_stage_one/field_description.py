"""字段发现模型使用的类型描述，以及到正式字段 output 的确定性编译器。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contract_processor.domain.models import FieldDefinition, OutputDefinition


OutputType = Literal[
    "string",
    "number",
    "integer",
    "boolean",
    "date",
    "enum",
    "object",
    "array",
]
SCALAR_OUTPUT_TYPES = {"string", "number", "integer", "boolean", "date"}


class StrictDescriptionModel(BaseModel):
    """模型字段描述拒绝额外键，避免把 JSON Schema 关键字混入业务描述。"""

    model_config = ConfigDict(extra="forbid")


class EnumValueDescription(StrictDescriptionModel):
    value: str = Field(min_length=1, max_length=80)
    meaning: str = Field(min_length=1, max_length=200)


class OutputPropertyDescription(StrictDescriptionModel):
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=2, max_length=80)
    meaning: str = Field(min_length=4, max_length=300)
    output: "OutputDescription"
    extraction_rule: str = Field(min_length=10, max_length=800)


class ArrayItemDescription(StrictDescriptionModel):
    name: str = Field(min_length=2, max_length=80)
    meaning: str = Field(min_length=4, max_length=300)
    output: "OutputDescription"
    extraction_rule: str = Field(min_length=10, max_length=800)


class OutputDescription(StrictDescriptionModel):
    """模型只描述值类型；正式 JSON Schema 关键字全部由程序编译。"""

    type: OutputType
    format: str | None = Field(default=None, max_length=200)
    unit: str | None = Field(default=None, max_length=80)
    minimum: float | None = None
    maximum: float | None = None
    pattern: str | None = Field(default=None, max_length=300)
    min_items: int | None = Field(default=None, ge=0)
    max_items: int | None = Field(default=None, ge=0)
    values: list[EnumValueDescription] | None = Field(
        default=None,
        min_length=1,
        max_length=40,
    )
    properties: list[OutputPropertyDescription] | None = Field(
        default=None, min_length=1, max_length=30
    )
    items: ArrayItemDescription | None = None

    @model_validator(mode="after")
    def validate_type_parameters(self) -> "OutputDescription":
        """让 Pydantic 运行时契约与提供给推理服务的分型 JSON Schema 保持一致。"""

        allowed = {
            "string": {"format", "pattern"},
            "date": {"format", "pattern"},
            "number": {"format", "unit", "minimum", "maximum"},
            "integer": {"format", "unit", "minimum", "maximum"},
            "boolean": {"format"},
            "enum": {"format", "values"},
            "object": {"format", "properties"},
            "array": {"format", "min_items", "max_items", "items"},
        }[self.type]
        parameter_names = {
            "format",
            "unit",
            "minimum",
            "maximum",
            "pattern",
            "min_items",
            "max_items",
            "values",
            "properties",
            "items",
        }
        invalid = sorted(
            name
            for name in parameter_names - allowed
            if getattr(self, name) is not None
        )
        if invalid:
            raise ValueError(f"output type={self.type} 不允许参数：{invalid}。")
        required_parameter = {"enum": "values", "object": "properties", "array": "items"}.get(
            self.type
        )
        if required_parameter and getattr(self, required_parameter) is None:
            raise ValueError(
                f"output type={self.type} 必须提供 {required_parameter}。"
            )
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: object, handler: object) -> dict:
        """按 type 输出互斥 oneOf，避免强 Schema 仍允许 string 携带 items/values。"""

        schema = handler(core_schema)  # type: ignore[operator]
        properties = schema["properties"]
        allowed = {
            "string": ("format", "pattern"),
            "date": ("format", "pattern"),
            "number": ("format", "unit", "minimum", "maximum"),
            "integer": ("format", "unit", "minimum", "maximum"),
            "boolean": ("format",),
            "enum": ("format", "values"),
            "object": ("format", "properties"),
            "array": ("format", "min_items", "max_items", "items"),
        }
        required_parameter = {"enum": "values", "object": "properties", "array": "items"}

        def without_null(value_schema: dict) -> dict:
            choices = value_schema.get("anyOf")
            if not isinstance(choices, list):
                return value_schema
            non_null = [item for item in choices if item.get("type") != "null"]
            return non_null[0] if len(non_null) == 1 else {"anyOf": non_null}

        variants = []
        for output_type, parameter_names in allowed.items():
            variant_properties = {
                "type": {
                    "const": output_type,
                    "title": "Type",
                    "type": "string",
                }
            }
            for name in parameter_names:
                parameter_schema = properties[name]
                if required_parameter.get(output_type) == name:
                    parameter_schema = without_null(parameter_schema)
                variant_properties[name] = parameter_schema
            required = ["type"]
            if output_type in required_parameter:
                required.append(required_parameter[output_type])
            variants.append(
                {
                    "type": "object",
                    "properties": variant_properties,
                    "required": required,
                    "additionalProperties": False,
                }
            )
        return {"title": schema.get("title", "OutputDescription"), "oneOf": variants}


OutputPropertyDescription.model_rebuild()
ArrayItemDescription.model_rebuild()
OutputDescription.model_rebuild()


OUTPUT_DESCRIPTION_PROMPT_RULES = """\
- output 是字段值的类型描述，不是 JSON Schema；禁止输出 nullable、required、
  additional_properties、anyOf、raw_value、reason 或 status。
- format 可用于说明任一类型的规范化格式；string/date 可补充 pattern，number/integer 可补充 unit、minimum、maximum。
- enum 必须提供非空 values，每项包含稳定枚举值 value 及其业务含义 meaning。
- object 必须提供非空 properties 列表；每个子字段包含 field_id、name、meaning、递归 output 和 extraction_rule。
- array 必须提供 items；items 包含 name、meaning、递归 output 和 extraction_rule。
- 不属于当前 type 的参数必须省略；所有 nullable、对象 required 和禁止额外属性规则由程序统一生成。"""


def _reject_present(
    description: OutputDescription, names: tuple[str, ...], *, path: str
) -> None:
    present = [name for name in names if getattr(description, name) is not None]
    if present:
        raise ValueError(f"{path} type={description.type} 不允许参数：{present}。")


def _common_record(description: OutputDescription, *, nullable: bool) -> dict[str, object]:
    record: dict[str, object] = {"type": description.type, "nullable": nullable}
    for name in ("format", "unit", "minimum", "maximum", "pattern", "min_items", "max_items"):
        value = getattr(description, name)
        if value is not None:
            record[name] = value
    return record


def compile_output_description(
    description: OutputDescription, *, nullable: bool = True, path: str = "output"
) -> dict[str, object]:
    """按 output.type 分发并编译正式字段 output；模型不参与 JSON Schema 细节生成。"""

    if description.minimum is not None and description.maximum is not None:
        if description.minimum > description.maximum:
            raise ValueError(f"{path}.minimum 不得大于 maximum。")
    if description.min_items is not None and description.max_items is not None:
        if description.min_items > description.max_items:
            raise ValueError(f"{path}.min_items 不得大于 max_items。")
    if description.format and re.match(r"^\s*pattern\s*[:：]", description.format, re.I):
        raise ValueError(
            f"{path}.format 不得用 'pattern: ...' 伪装正则约束；"
            "需要正则时必须写入 output.pattern。"
        )

    output_type = description.type
    if output_type in SCALAR_OUTPUT_TYPES:
        _reject_present(
            description,
            ("values", "properties", "items", "min_items", "max_items"),
            path=path,
        )
        if output_type not in {"number", "integer"}:
            _reject_present(description, ("unit", "minimum", "maximum"), path=path)
        if output_type not in {"string", "date"}:
            _reject_present(description, ("pattern",), path=path)
        return _common_record(description, nullable=nullable)

    if output_type == "enum":
        _reject_present(
            description,
            (
                "unit",
                "properties",
                "items",
                "minimum",
                "maximum",
                "pattern",
                "min_items",
                "max_items",
            ),
            path=path,
        )
        if not description.values:
            raise ValueError(f"{path}.values 必须是非空枚举描述。")
        values: dict[str, str] = {}
        for item in description.values:
            key = item.value.strip()
            if key in values:
                raise ValueError(f"{path}.values 包含重复枚举值：{key}。")
            values[key] = item.meaning.strip()
        return {**_common_record(description, nullable=nullable), "values": values}

    if output_type == "object":
        _reject_present(
            description,
            (
                "unit",
                "values",
                "items",
                "minimum",
                "maximum",
                "pattern",
                "min_items",
                "max_items",
            ),
            path=path,
        )
        if not description.properties:
            raise ValueError(f"{path}.properties 必须是非空子字段描述列表。")
        properties: dict[str, object] = {}
        for item in description.properties:
            property_id = item.field_id.strip()
            if property_id in properties:
                raise ValueError(f"{path}.properties 包含重复 field_id：{property_id}。")
            child = compile_output_description(
                item.output,
                nullable=True,
                path=f"{path}.properties.{property_id}",
            )
            properties[property_id] = {
                **child,
                "name": item.name.strip(),
                "meaning": item.meaning.strip(),
                "extraction_rule": item.extraction_rule.strip(),
            }
        return {
            **_common_record(description, nullable=nullable),
            "properties": properties,
            "required": list(properties),
            "additional_properties": False,
        }

    if output_type == "array":
        _reject_present(
            description,
            ("unit", "values", "properties", "minimum", "maximum", "pattern"),
            path=path,
        )
        if description.items is None:
            raise ValueError(f"{path}.items 必须是完整元素描述。")
        item_output = compile_output_description(
            description.items.output,
            # 数组本身可空；一旦元素存在，元素不得再以 null 占位。
            nullable=False,
            path=f"{path}.items",
        )
        return {
            **_common_record(description, nullable=nullable),
            "items": {
                **item_output,
                "name": description.items.name.strip(),
                "meaning": description.items.meaning.strip(),
                "extraction_rule": description.items.extraction_rule.strip(),
            },
        }

    raise ValueError(f"{path}.type 不支持：{output_type!r}。")


def _output_definition_record(output: OutputDefinition) -> dict[str, object]:
    record: dict[str, object] = {"type": output.type, "nullable": output.nullable}
    for name in (
        "format",
        "name",
        "meaning",
        "unit",
        "extraction_rule",
        "minimum",
        "maximum",
        "pattern",
        "min_items",
        "max_items",
        "min_length",
        "max_length",
    ):
        value = getattr(output, name)
        if value is not None:
            record[name] = value
    if output.not_meaning:
        record["not_meaning"] = list(output.not_meaning)
    if output.enum_descriptions:
        record["values"] = dict(output.enum_descriptions)
    elif output.enum_values:
        record["values"] = list(output.enum_values)
    if output.properties:
        record["properties"] = {
            name: _output_definition_record(child) for name, child in output.properties
        }
        record["required"] = list(output.required)
        record["additional_properties"] = output.additional_properties
    if output.items is not None:
        record["items"] = _output_definition_record(output.items)
    return record


def field_definition_record(definition: FieldDefinition) -> dict[str, object]:
    """把领域定义投影为模型理解所需的规范记录；不携带 examples。"""

    return {
        "field_id": definition.field_id,
        "name": definition.name,
        "meaning": definition.meaning,
        "aliases": list(definition.aliases),
        "not_meaning": list(definition.not_meaning),
        "output": _output_definition_record(definition.output),
        "extraction_rule": definition.extraction_rule,
    }


def _render_output_card(output: dict[str, object], *, indent: int) -> list[str]:
    prefix = " " * indent
    nullable = "允许空" if output.get("nullable") is True else "不可空"
    lines = [f"{prefix}类型：{output.get('type')}（{nullable}）"]
    if output.get("format"):
        lines.append(f"{prefix}格式：{output['format']}")
    if output.get("unit"):
        lines.append(f"{prefix}单位：{output['unit']}")
    if output.get("meaning"):
        lines.append(f"{prefix}含义：{output['meaning']}")
    if output.get("not_meaning"):
        lines.append(f"{prefix}明确排除：{'、'.join(output['not_meaning'])}")
    if output.get("values"):
        values = output["values"]
        if isinstance(values, dict):
            rendered = "；".join(f"{key}={meaning}" for key, meaning in values.items())
        else:
            rendered = "、".join(str(value) for value in values)
        lines.append(f"{prefix}枚举：{rendered}")
    properties = output.get("properties")
    if isinstance(properties, dict):
        lines.append(f"{prefix}子字段（固定存在，值可按各自定义为空）：")
        for property_id, child in properties.items():
            assert isinstance(child, dict)
            label = child.get("name") or property_id
            lines.append(f"{prefix}- {property_id}｜{label}")
            lines.extend(_render_output_card(child, indent=indent + 2))
    items = output.get("items")
    if isinstance(items, dict):
        lines.append(f"{prefix}数组元素：{items.get('name') or '单个元素'}")
        lines.extend(_render_output_card(items, indent=indent + 2))
    if output.get("extraction_rule"):
        lines.append(f"{prefix}提取规则：{output['extraction_rule']}")
    return lines


def render_field_card(record: dict[str, object]) -> str:
    """以紧凑语义卡向模型展示字段；空治理列表与 examples 不占用注意力。"""

    lines = [
        f"字段：{record['field_id']}｜{record['name']}",
        f"含义：{record['meaning']}",
    ]
    aliases = record.get("aliases")
    if isinstance(aliases, list) and aliases:
        lines.append("常见称谓：" + "、".join(str(value) for value in aliases))
    not_meaning = record.get("not_meaning")
    if isinstance(not_meaning, list) and not_meaning:
        lines.append("明确排除：" + "、".join(str(value) for value in not_meaning))
    lines.append("输出：")
    output = record["output"]
    assert isinstance(output, dict)
    lines.extend(_render_output_card(output, indent=2))
    lines.append(f"字段提取规则：{record['extraction_rule']}")
    return "\n".join(lines)
