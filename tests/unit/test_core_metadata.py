"""Core 元数据结构与统一值包络的回归测试。"""

from pathlib import Path

import pytest
import yaml

from contract_processor.application.prompts.core_fields import build_compact_field_prompt
from contract_processor.application.schemas.core_extraction import (
    build_core_extraction_schema,
)
from contract_processor.domain.enums import ExtractionStatus, FieldKind
from contract_processor.domain.models import FieldObservation
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_YAML = PROJECT_ROOT / "description/fields/core/core.yaml"


def observation(
    *,
    status: ExtractionStatus,
    value: object = None,
) -> FieldObservation:
    return FieldObservation(
        field_id="example",
        name="示例",
        meaning="示例字段",
        status=status,
        value=value,
        raw_value=None,
        contract_id="contract-1",
    )


def test_core_catalog_uses_new_semantic_dimensions() -> None:
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    field_ids = [field["field_id"] for field in payload["fields"]]

    assert payload["schema_version"] == "0.9"
    assert len(field_ids) == len(set(field_ids))
    assert {"document_role", "transaction_type", "contract_form"} <= set(field_ids)
    assert {"effective_mechanism", "contract_validity_period"} <= set(field_ids)
    assert {"contract_type", "effective_date", "contract_term"}.isdisjoint(field_ids)


def test_complex_outputs_have_recursive_child_definitions() -> None:
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))

    for field in payload["fields"]:
        output = field["output"]
        if output["type"] == "object":
            assert output["properties"]
            assert set(output["required"]) == set(output["properties"])
            assert output["additional_properties"] is False
        if output["type"] == "array":
            assert output["items"]


def test_generated_schema_strongly_types_contract_amount() -> None:
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    schema = build_core_extraction_schema(payload["fields"])
    fields_schema = schema["properties"]["fields"]
    amount_field = fields_schema["properties"]["contract_amount"]
    amount_properties = amount_field["properties"]["properties"]
    amount_type_value = amount_properties["properties"]["amount_type"]["properties"]["value"]
    amount_type_schema = next(
        item for item in amount_type_value["anyOf"] if item.get("type") == "string"
    )

    assert fields_schema["required"] == [field["field_id"] for field in payload["fields"]]
    assert fields_schema["additionalProperties"] is False
    assert amount_field["required"] == ["properties"]
    assert set(amount_properties["required"]) == set(amount_properties["properties"])
    assert amount_properties["additionalProperties"] is False
    assert "tax_amount" not in amount_properties["properties"]
    assert "tax_amount" not in amount_properties["required"]
    assert "tax_exclusive_amount" not in amount_properties["properties"]
    assert "tax_exclusive_amount" not in amount_properties["required"]
    assert amount_type_schema["enum"] == [
        "fixed_total",
        "estimated",
        "ceiling",
        "settlement_based",
        "unit_price_only",
        "framework_no_total",
        "unknown",
    ]
    assert {"type": "null"} in amount_type_value["anyOf"]


def test_subject_matter_items_include_nullable_brand() -> None:
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    schema = build_core_extraction_schema(payload["fields"])
    subject_properties = (
        schema["properties"]["fields"]["properties"]["subject_matter"]["properties"][
            "properties"
        ]
    )
    items_value = subject_properties["properties"]["items"]["properties"]["value"]
    items_schema = next(
        item for item in items_value["anyOf"] if item.get("type") == "array"
    )
    item_schema = items_schema["items"]
    brand_schema = item_schema["properties"]["brand"]

    # 业务上的可选字段仍固定输出键，以 null 明确表示合同没有可靠品牌信息。
    assert item_schema["required"] == [
        "source_name",
        "normalized_name",
        "brand",
        "model",
    ]
    assert brand_schema["anyOf"][0]["type"] == "string"
    assert brand_schema["anyOf"][1] == {"type": "null"}
    assert item_schema["additionalProperties"] is False


def test_generated_amount_schema_rejects_missing_or_mistyped_children() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    schema = build_core_extraction_schema(payload["fields"])
    amount_schema = schema["properties"]["fields"]["properties"]["contract_amount"]
    validator = jsonschema.Draft202012Validator(amount_schema)

    errors = list(
        validator.iter_errors(
            {
                "properties": {
                    "amount": {
                        "raw_value": "一万元",
                        "status": "found",
                        "value": "一万元",
                        "reason": "合同总额明确。",
                    }
                }
            }
        )
    )

    assert errors
    assert any(error.validator in {"required", "type"} for error in errors)


def test_generated_envelope_schema_keeps_ordered_scalar_shape() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    signing_date = next(
        field for field in payload["fields"] if field["field_id"] == "signing_date"
    )
    schema = build_core_extraction_schema([signing_date])
    envelope_schema = schema["properties"]["fields"]["properties"]["signing_date"]
    validator = jsonschema.Draft202012Validator(envelope_schema)
    found_with_null = {
        "raw_value": None,
        "reason": "合同明确约定该日期。",
        "status": "found",
        "value": None,
    }

    assert "allOf" not in envelope_schema
    assert envelope_schema["required"] == [
        "raw_value",
        "reason",
        "status",
        "value",
    ]
    assert list(envelope_schema["properties"]) == [
        "raw_value",
        "reason",
        "status",
        "value",
    ]
    assert schema["required"] == [
        "document_id",
        "fields",
    ]
    assert list(schema["properties"]) == ["document_id", "fields"]
    # 平坦生成 Schema 只负责单属性结构；found/null 矛盾留给应用层业务校验。
    assert not list(validator.iter_errors(found_with_null))


def test_object_schema_wraps_each_direct_property_in_decision_envelope() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    payload = yaml.safe_load(CORE_YAML.read_text(encoding="utf-8"))
    validity = next(
        field
        for field in payload["fields"]
        if field["field_id"] == "contract_validity_period"
    )
    schema = build_core_extraction_schema([validity])
    envelope_schema = (
        schema["properties"]["fields"]["properties"]["contract_validity_period"]
    )
    property_schemas = envelope_schema["properties"]["properties"]
    valid = {
        "properties": {
            name: {
                "raw_value": None,
                "reason": "合同未约定该子字段。",
                "status": "not_found",
                "value": None,
            }
            for name in ("start_date", "end_date", "duration_text", "auto_renewal")
        }
    }

    errors = list(jsonschema.Draft202012Validator(envelope_schema).iter_errors(valid))

    assert not errors
    assert property_schemas["required"] == [
        "start_date",
        "end_date",
        "duration_text",
        "auto_renewal",
    ]
    for child in property_schemas["properties"].values():
        assert child["required"] == ["raw_value", "reason", "status", "value"]


def test_field_catalog_preserves_recursive_output_definitions(tmp_path: Path) -> None:
    empty_attributes = tmp_path / "attribute.yaml"
    empty_attributes.write_text("fields: []\n", encoding="utf-8")
    catalog = YamlFieldCatalog(core_path=CORE_YAML, attribute_path=empty_attributes)
    definitions = {item.field_id: item for item in catalog.load(FieldKind.CORE)}

    amount = definitions["contract_amount"].output
    assert amount.property("tax_rate").unit == "percent"
    assert amount.property("tax_rate").maximum == 100
    assert amount.property("amount_type").enum_values[0] == "fixed_total"
    effective = definitions["effective_mechanism"].output
    assert "on_signing_and_seal" in effective.property("trigger_type").enum_values
    assert {name for name, _ in effective.properties} == {
        "date",
        "trigger_type",
        "trigger_text",
    }
    subject_items = definitions["subject_matter"].output.property("items").items
    assert subject_items is not None
    assert subject_items.property("brand").nullable is True
    assert "不得依据型号、生产厂家、供应商名称或外部知识推断品牌" in (
        subject_items.property("brand").extraction_rule or ""
    )

    prompt = build_compact_field_prompt(definitions.values())
    assert "tax_rate:" in prompt
    assert "输出13表示13%而不是0.13" in prompt
    # 金额口径硬规则必须进入实际紧凑提示词，不能只存在于人工说明或 examples 中。
    assert "原文优先于计算和推断" in prompt
    assert "未税价格、未税总额和不含税总价不再作为独立 Core 子字段输出" in prompt
    assert "相关原文必须保留在 source_amount_text" in prompt
    assert "费用范围不同或不明确时禁止反推隐含税率" in prompt
    assert "tax_amount" not in prompt
    assert "tax_exclusive_amount" not in prompt
    assert "on_signing_and_seal" in prompt
    assert "brand:" in prompt
    assert "仅在合同原文明示品牌或厂牌" in prompt
    assert "签订日期或生效日期本身不能作为 start_date" in prompt


def test_found_observation_requires_value() -> None:
    with pytest.raises(ValueError, match="非空 value"):
        observation(status=ExtractionStatus.FOUND)

    assert observation(
        status=ExtractionStatus.FOUND, value="设备采购合同"
    ).value == "设备采购合同"


def test_ambiguous_and_conflicting_states_do_not_require_candidate_payloads() -> None:
    assert observation(status=ExtractionStatus.AMBIGUOUS).value is None
    assert observation(status=ExtractionStatus.CONFLICTING).value is None
