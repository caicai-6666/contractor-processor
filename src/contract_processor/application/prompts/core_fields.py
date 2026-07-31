"""将字段定义压缩为适合 MLLM 注入的文本。"""

from collections.abc import Iterable

from contract_processor.domain.models import FieldDefinition, OutputDefinition


def _render_output_definition(output: OutputDefinition, indent: int) -> list[str]:
    """递归渲染复杂值的必要语义，避免提示词只剩无约束的 object。"""

    prefix = " " * indent
    lines = [
        f"{prefix}type: {output.type}",
        f"{prefix}nullable: {str(output.nullable).lower()}",
    ]
    if output.format:
        lines.append(f"{prefix}format: {output.format}")
    if output.meaning:
        lines.append(f"{prefix}meaning: {output.meaning}")
    if output.not_meaning:
        lines.append(f"{prefix}not_meaning: {'、'.join(output.not_meaning)}")
    if output.extraction_rule:
        lines.append(f"{prefix}rule: {output.extraction_rule}")
    if output.enum_descriptions:
        values = "；".join(f"{key}={meaning}" for key, meaning in output.enum_descriptions)
        lines.append(f"{prefix}values: {values}")
    if output.required:
        lines.append(f"{prefix}required: {', '.join(output.required)}")
    if output.properties:
        lines.append(f"{prefix}properties:")
        for name, child in output.properties:
            lines.append(f"{prefix}  {name}:")
            lines.extend(_render_output_definition(child, indent + 4))
    if output.items is not None:
        lines.append(f"{prefix}items:")
        lines.extend(_render_output_definition(output.items, indent + 2))
    return lines


def build_compact_field_prompt(fields: Iterable[FieldDefinition]) -> str:
    """递归注入抽取关键约束，同时省略 examples 等高占用内容。"""

    entries = []
    for field in fields:
        aliases = "、".join(field.aliases)
        lines = [
            f"- field_id: {field.field_id}",
            f"  name: {field.name}",
            f"  meaning: {field.meaning}",
            f"  aliases: {aliases}",
            f"  not_meaning: {'、'.join(field.not_meaning)}",
            "  output:",
        ]
        lines.extend(_render_output_definition(field.output, 4))
        lines.append(f"  rule: {field.extraction_rule}")
        entries.append("\n".join(lines))
    return "\n".join(entries)
