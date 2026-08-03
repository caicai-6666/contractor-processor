"""从项目字段 YAML 读取领域字段定义。"""

from pathlib import Path
from typing import Any

import yaml

from contract_processor.async_utils import run_blocking
from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import (
    FieldCatalogSnapshot,
    FieldDefinition,
    FieldExample,
    OutputDefinition,
)


class YamlFieldCatalog:
    """字段 YAML 的本地实现；调用方无需了解文件布局。"""

    def __init__(self, *, core_path: Path, attribute_path: Path) -> None:
        self._paths = {FieldKind.CORE: core_path, FieldKind.ATTRIBUTE: attribute_path}

    async def load(self, kind: FieldKind) -> list[FieldDefinition]:
        """在线程中完成小型配置文件读取，避免阻塞事件循环。"""

        snapshot = await self.snapshot(kind)
        return list(snapshot.definitions)

    async def snapshot(self, kind: FieldKind) -> FieldCatalogSnapshot:
        """读取并校验本次运行使用的稳定字段目录快照。"""

        return await run_blocking(self._load_snapshot_sync, kind)

    def _load_snapshot_sync(self, kind: FieldKind) -> FieldCatalogSnapshot:
        path = self._paths[kind]
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise RuntimeError(f"{kind.value} 字段目录根节点必须是对象。")
        if payload.get("field_set") != kind.value:
            raise RuntimeError(
                f"{kind.value} 字段目录的 field_set 必须为 {kind.value}。"
            )
        if "schema_version" not in payload:
            raise RuntimeError(f"{kind.value} 字段目录缺少 schema_version。")
        if "status" not in payload or not isinstance(payload["status"], str):
            raise RuntimeError(f"{kind.value} 字段目录必须声明字符串 status。")
        records = payload.get("fields")
        if not isinstance(records, list):
            raise RuntimeError(f"{kind.value} 字段目录的 fields 必须是数组。")
        status = payload["status"]
        if status == "empty" and records:
            raise RuntimeError(
                f"{kind.value} 字段目录 status=empty 时 fields 必须为空。"
            )
        if status != "empty" and not records:
            raise RuntimeError(
                f"{kind.value} 字段目录没有字段时必须显式声明 status=empty。"
            )
        definitions = tuple(self._to_definition(record, kind) for record in records)
        field_ids = [definition.field_id for definition in definitions]
        if len(field_ids) != len(set(field_ids)):
            raise RuntimeError(f"{kind.value} 字段目录包含重复 field_id。")
        return FieldCatalogSnapshot(
            kind=kind,
            schema_version=str(payload["schema_version"]),
            status=status,
            definitions=definitions,
        )

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
