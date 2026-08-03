"""Attribute 初始目录及递归字段契约的回归测试。"""

import asyncio
from pathlib import Path
from typing import Any

import yaml

from contract_processor.domain.enums import FieldKind
from contract_processor.application.schemas.core_extraction import (
    build_field_extraction_schema,
)
from contract_processor.infrastructure.extraction.attribute.pipeline import (
    render_compact_core_context,
)
from contract_processor.infrastructure.persistence.yaml_field_catalog import (
    YamlFieldCatalog,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_YAML = PROJECT_ROOT / "data/definitions/core.yaml"
ATTRIBUTE_YAML = PROJECT_ROOT / "data/definitions/attribute.yaml"
ATTRIBUTE_PROMPT = (
    PROJECT_ROOT
    / "src/contract_processor/infrastructure/extraction/attribute/prompts"
    / "01_extract_attribute_field.txt"
)

EXPECTED_FIELD_IDS = {
    "order_numbers",
    "project_numbers",
    "delivery_commitment",
    "delivery_locations",
    "payment_schedule",
    "invoice_requirement",
    "acceptance_mechanism",
    "warranty_commitment",
    "performance_security",
    "dispute_resolution",
}


def _assert_recursive_output_contract(output: dict[str, Any]) -> None:
    """所有递归节点都必须显式表达类型和空值，复杂节点不得依赖格式字符串。"""

    assert output["type"] in {
        "string",
        "number",
        "integer",
        "boolean",
        "date",
        "enum",
        "object",
        "array",
    }
    assert isinstance(output["nullable"], bool)

    if output["type"] == "object":
        assert output["properties"]
        assert set(output["required"]) == set(output["properties"])
        assert output["additional_properties"] is False
        for child in output["properties"].values():
            _assert_recursive_output_contract(child)
    if output["type"] == "array":
        assert output["items"]
        _assert_recursive_output_contract(output["items"])
    if output["type"] == "enum":
        assert output["values"]


def test_attribute_catalog_contains_expert_seed_definitions() -> None:
    payload = yaml.safe_load(ATTRIBUTE_YAML.read_text(encoding="utf-8"))
    field_ids = [field["field_id"] for field in payload["fields"]]

    assert payload["schema_version"] == "0.3"
    assert payload["field_set"] == "attribute"
    assert payload["status"] == "draft"
    assert len(field_ids) == len(set(field_ids)) == 10
    assert set(field_ids) == EXPECTED_FIELD_IDS


def test_attribute_outputs_satisfy_recursive_definition_contract() -> None:
    payload = yaml.safe_load(ATTRIBUTE_YAML.read_text(encoding="utf-8"))

    for field in payload["fields"]:
        _assert_recursive_output_contract(field["output"])


def test_yaml_catalog_loads_attribute_constraints_without_loss() -> None:
    catalog = YamlFieldCatalog(core_path=CORE_YAML, attribute_path=ATTRIBUTE_YAML)
    snapshot = asyncio.run(catalog.snapshot(FieldKind.ATTRIBUTE))
    definitions = {definition.field_id: definition for definition in snapshot.definitions}

    assert snapshot.status == "draft"
    assert snapshot.schema_version == "0.3"
    assert snapshot.field_count == 10
    assert definitions["payment_schedule"].output.items is not None
    payment_item = definitions["payment_schedule"].output.items
    assert payment_item is not None
    assert payment_item.property("ratio").unit == "percent"
    assert payment_item.property("ratio").maximum == 100
    assert definitions["invoice_requirement"].output.property(
        "invoice_type"
    ).enum_values == (
        "vat_special",
        "vat_general",
        "electronic",
        "receipt",
        "other",
    )
    assert "不能作为发票类型或开票税率的合同证据" in definitions[
        "invoice_requirement"
    ].extraction_rule
    assert "项目名称”即使含有编码式片段也不构成项目编号" in definitions[
        "project_numbers"
    ].extraction_rule
    assert "验收款支付期限" in definitions["acceptance_mechanism"].not_meaning
    assert "关系性地域描述不是机构名称" in definitions[
        "dispute_resolution"
    ].output.property("institution_name").extraction_rule


def test_seed_attributes_do_not_duplicate_core_field_ids() -> None:
    core_payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    attribute_payload = yaml.safe_load(ATTRIBUTE_YAML.read_text(encoding="utf-8"))
    core_ids = {field["field_id"] for field in core_payload["fields"]}
    attribute_ids = {field["field_id"] for field in attribute_payload["fields"]}

    assert core_ids.isdisjoint(attribute_ids)


def test_attribute_schema_is_closed_and_uses_attribute_descriptions() -> None:
    payload = yaml.safe_load(ATTRIBUTE_YAML.read_text(encoding="utf-8"))
    schema = build_field_extraction_schema(
        payload["fields"], field_set_name="Attribute"
    )
    fields_schema = schema["properties"]["fields"]

    assert fields_schema["required"] == [field["field_id"] for field in payload["fields"]]
    assert fields_schema["additionalProperties"] is False
    assert "Attribute 对象字段" in fields_schema["properties"][
        "delivery_commitment"
    ]["description"]


def test_attribute_prompt_requires_reason_to_commit_status_and_value() -> None:
    prompt = ATTRIBUTE_PROMPT.read_text(encoding="utf-8")

    assert "严格按 raw_value、reason、status、value 输出" in prompt
    assert "所以接下来的 status=found，value=非 null。" in prompt
    assert "所以接下来的 status=状态名，value=null。" in prompt
    assert "随后输出的 status 和 value 必须与该决定完全一致" in prompt
    assert "整个 reason（含固定输出决定）不得超过 300 个字符" in prompt
    assert "不得展开完整思维链" in prompt


def test_compact_core_context_only_exposes_successful_normalized_values() -> None:
    catalog = YamlFieldCatalog(core_path=CORE_YAML, attribute_path=ATTRIBUTE_YAML)
    core_definitions = list(asyncio.run(catalog.load(FieldKind.CORE)))
    context = render_compact_core_context(
        {
            "contract_title": {
                "raw_value": "设备采购合同",
                "reason": "标题明确。",
                "status": "found",
                "value": "设备采购合同",
            },
            "contract_number": {
                "raw_value": None,
                "reason": "未找到。",
                "status": "not_found",
                "value": None,
            },
            "contract_amount": {
                "status": "found",
                "properties": {
                    "amount": {
                        "raw_value": "10000元",
                        "reason": "金额明确。",
                        "status": "found",
                        "value": 10000,
                    },
                    "currency": {
                        "raw_value": None,
                        "reason": "未明确。",
                        "status": "not_found",
                        "value": None,
                    },
                },
            },
        },
        core_definitions,
    )

    assert "合同名称（contract_title）：\"设备采购合同\"" in context
    assert "合同金额（contract_amount）：{\"amount\":10000}" in context
    assert "contract_number" not in context
    assert "raw_value" not in context
