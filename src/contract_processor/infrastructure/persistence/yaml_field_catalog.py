"""从项目字段 YAML 读取领域字段定义。"""

from pathlib import Path
from typing import Any

import yaml

from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import FieldDefinition, FieldExample, OutputDefinition


class YamlFieldCatalog:
    """字段 YAML 的本地实现；调用方无需了解文件布局。"""

    def __init__(self, *, core_path: Path, attribute_path: Path) -> None:
        self._paths = {FieldKind.CORE: core_path, FieldKind.ATTRIBUTE: attribute_path}

    def load(self, kind: FieldKind) -> list[FieldDefinition]:
        path = self._paths[kind]
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        records = payload.get("fields", [])
        return [self._to_definition(record, kind) for record in records]

    @staticmethod
    def _to_output_definition(output: dict[str, Any]) -> OutputDefinition:
        """递归保留对象和数组的子字段约束，确保 YAML 是唯一规范源。"""

        values = output.get("values", {})
        enum_values = tuple(values) if isinstance(values, dict) else tuple(values)
        enum_descriptions = (
            tuple((str(value), str(description)) for value, description in values.items())
            if isinstance(values, dict)
            else ()
        )
        properties = tuple(
            (name, YamlFieldCatalog._to_output_definition(child))
            for name, child in output.get("properties", {}).items()
        )
        items = output.get("items")
        return OutputDefinition(
            type=output["type"],
            format=output.get("format"),
            nullable=output["nullable"],
            example=output.get("example"),
            name=output.get("name"),
            meaning=output.get("meaning"),
            unit=output.get("unit"),
            not_meaning=tuple(output.get("not_meaning", [])),
            extraction_rule=output.get("extraction_rule"),
            enum_values=enum_values,
            enum_descriptions=enum_descriptions,
            properties=properties,
            required=tuple(output.get("required", [])),
            additional_properties=output.get("additional_properties", False),
            items=YamlFieldCatalog._to_output_definition(items) if items else None,
            minimum=output.get("minimum"),
            maximum=output.get("maximum"),
            pattern=output.get("pattern"),
            min_items=output.get("min_items"),
            max_items=output.get("max_items"),
            min_length=output.get("min_length"),
            max_length=output.get("max_length"),
        )

    @staticmethod
    def _to_definition(record: dict[str, Any], kind: FieldKind) -> FieldDefinition:
        examples = tuple(
            FieldExample(source_text=item["source_text"], output=item.get("output"))
            for item in record.get("examples", [])
        )
        return FieldDefinition(
            field_id=record["field_id"],
            name=record["name"],
            meaning=record["meaning"],
            aliases=tuple(record.get("aliases", [])),
            not_meaning=tuple(record.get("not_meaning", [])),
            output=YamlFieldCatalog._to_output_definition(record["output"]),
            extraction_rule=record["extraction_rule"],
            examples=examples,
            kind=kind,
        )
