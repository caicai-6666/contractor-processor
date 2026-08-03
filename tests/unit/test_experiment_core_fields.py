"""Core 提取实验逐字段调用与前缀布局的回归测试。"""

import importlib.util
import inspect
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from contract_processor.application.schemas.core_extraction import (
    build_core_extraction_schema,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_PATH = (
    PROJECT_ROOT
    / "src/contract_processor/infrastructure/extraction/core/pipeline.py"
)
STEP_1_PROMPT_PATH = (
    PROJECT_ROOT
    / "src/contract_processor/infrastructure/extraction/core/prompts/01_understand_contract.txt"
)
STEP_2_PROMPT_PATH = (
    PROJECT_ROOT
    / "src/contract_processor/infrastructure/extraction/core/prompts/02_extract_core.txt"
)


def load_experiment_module():
    spec = importlib.util.spec_from_file_location("core_field_extraction_experiment", RUN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Pydantic 解析 future annotations 时需要能从 sys.modules 找到实验模块。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_production_core_pipeline_is_async_and_fixed_to_single_field_mode() -> None:
    experiment = load_experiment_module()

    assert inspect.iscoroutinefunction(experiment.run_core_extraction)
    assert experiment.STEP_2_FIELD_MAX_COMPLETION_TOKENS == 6144


def test_model_candidate_cannot_output_document_id() -> None:
    experiment = load_experiment_module()

    properties = experiment.CoreExtractionCandidate.model_json_schema()["properties"]

    assert list(properties) == ["fields"]
    with pytest.raises(ValidationError):
        experiment.CoreExtractionCandidate.model_validate(
            {"document_id": "a" * 64, "fields": {}}
        )


def test_final_core_result_requires_sha256_document_id() -> None:
    experiment = load_experiment_module()

    result = experiment.CoreExtraction(document_id="a" * 64, fields={})

    assert result.document_id == "a" * 64
    with pytest.raises(ValidationError):
        experiment.CoreExtraction(document_id="HT-001", fields={})


def test_messages_place_variable_schema_suffix_after_images() -> None:
    experiment = load_experiment_module()
    messages = experiment.messages_for(
        "公共提示词",
        [{"data_url": "data:image/png;base64,AA=="}],
        "当前批字段定义",
    )

    content = messages[1]["content"]
    assert [item["type"] for item in content] == ["text", "image_url", "text"]
    assert content[0]["text"] == "公共提示词"
    assert content[2]["text"] == "当前批字段定义"


def test_step1_amount_mentions_are_rendered_as_readable_bullets() -> None:
    experiment = load_experiment_module()
    schema = experiment.ContractUnderstanding.model_json_schema()
    understanding = experiment.ContractUnderstanding.model_validate(
        {
            "document_overview": {
                "is_contract": True,
                "contract_type_guess": "销售合同",
                "language": "zh-CN",
                "page_count": 1,
                "summary": "销售合同。",
            },
            "parties_hint": [],
            "page_map": [
                {
                    "page": 1,
                    "section_or_topic": "产品与价格表",
                    "summary": "包含含税价、未税价和总额。",
                    "quality_notes": [],
                }
            ],
            "information_locations": [],
            "amount_and_fee_mentions": [
                {
                    "page": 1,
                    "category": "freight",
                    "source_text": "本报价含13%增值税专用发票，含运费",
                    "scope_or_context": "合同报价费用组成",
                }
            ],
            "risks_and_conflicts": [],
            "unresolved_items": [],
        }
    )

    rendered = experiment.render_contract_understanding_bullets(understanding)

    assert "amount_and_fee_mentions" in schema["required"]
    assert rendered.startswith("- 文档概览：")
    assert "- 金额与费用原文清单：" in rendered
    assert "- 类别：freight" in rendered
    assert "- 原文：本报价含13%增值税专用发票，含运费" in rendered
    assert not rendered.lstrip().startswith("{")


def test_prompts_require_complete_amount_handoff_between_steps() -> None:
    step1_prompt = STEP_1_PROMPT_PATH.read_text(encoding="utf-8")
    step2_prompt = STEP_2_PROMPT_PATH.read_text(encoding="utf-8")
    core_catalog = (PROJECT_ROOT / "data/definitions/core.yaml").read_text(
        encoding="utf-8"
    )

    assert "金额与费用穷举" in step1_prompt
    assert "amount_and_fee_mentions 必须覆盖可见页面中的全部金额与费用原文" in step1_prompt
    assert "{{CONTRACT_UNDERSTANDING_BULLETS}}" in step2_prompt
    assert "当前字段涉及金额、税率或费用时" in step2_prompt
    assert "核对“合同理解条目”中的“金额与费用原文清单”" in step2_prompt
    assert "每一个计算输入都必须同时得到该清单和图像支持" in core_catalog
    assert "不得仅因金额位于产品行就推断为单价" in step1_prompt
    assert "tax_exclusive_price/tax_inclusive_amount" in step1_prompt
    assert "不再作为独立 Core 子字段输出" in core_catalog
    assert "必须保留在 source_amount_text" in core_catalog


def test_sampling_parameters_disable_tool_protocol_markers() -> None:
    experiment = load_experiment_module()
    parameters = experiment.build_sampling_parameters(
        {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
            "repetition_penalty": 1.0,
            "seed": 3407,
        }
    )

    assert parameters["extra_body"]["bad_words"] == [
        "<tool_call>",
        "</tool_call>",
        "<tool_response>",
        "</tool_response>",
    ]


def test_generated_core_envelope_places_reason_before_status() -> None:
    experiment = load_experiment_module()
    schema = build_core_extraction_schema(
        [
            {
                "field_id": "example",
                "output": {"type": "string", "nullable": True},
            }
        ]
    )
    envelope_schema = schema["properties"]["fields"]["properties"]["example"]

    assert list(envelope_schema["properties"]) == [
        "raw_value",
        "reason",
        "status",
        "value",
    ]
    parsed = experiment.CoreFieldValue(
        raw_value="示例值",
        reason="合同原文明示该值。",
        status="found",
        value="示例值",
    )
    assert parsed.value == "示例值"


def test_step2_prompt_defines_object_property_envelopes_and_reason_order() -> None:
    prompt = STEP_2_PROMPT_PATH.read_text(encoding="utf-8")

    assert "提取当前唯一字段" in prompt
    assert "只包含当前字段定义中的唯一 field_id" in prompt
    assert "顶层只输出 fields" in prompt
    assert "不得输出 document_id" in prompt
    assert "不得生成根级 reason" in prompt
    assert "每个直属子字段严格按 raw_value、reason、status、value 的顺序" in prompt
    assert "对象外层 status 由程序汇总" in prompt
    assert "out_of_scope" in prompt
    # 单字段公共 Prompt 不携带其他 Core 的专有规则；它们由当前字段 YAML 按需注入。
    assert "contract_number 是可空业务字段" not in prompt
    assert "contract_validity_period 只接收" not in prompt
    assert "effective_mechanism.trigger_type" not in prompt
    assert "contract_amount 不输出" not in prompt
    assert "tax_exclusive_amount" not in prompt
    assert "not_found/not_applicable 的 value 必须为 null" in prompt
    assert "raw_value 可为 null 或最小相关原文" in prompt
    assert "raw_value 不得填写 lower_snake_case 枚举代码" in prompt
    assert "所以接下来的 status=found，value=非 null。" in prompt
    assert "所以接下来的 status=状态名，value=null。" in prompt
    assert "随后输出的 status 和 value 必须与该决定完全一致" in prompt
    assert "整个 reason（含固定输出决定）不得超过 300 个字符" in prompt


def test_effective_mechanism_catalog_forbids_signature_layout_inference() -> None:
    catalog = (PROJECT_ROOT / "data/definitions/core.yaml").read_text(
        encoding="utf-8"
    )

    assert "仅看见签名、印章或签署区域不得推断生效机制" in catalog
    assert "签署区的主体、代表人、签名和印章不是生效条件原文" in catalog


def test_conditional_effective_date_without_clause_provenance_is_removed() -> None:
    experiment = load_experiment_module()
    candidate = experiment.ObjectFieldCandidate(
        properties={
            "date": experiment.ObjectPropertyValue(
                raw_value="2025/9/8",
                reason="由签订日期推定。",
                status="found",
                value="2025-09-08",
            ),
            "trigger_type": experiment.ObjectPropertyValue(
                raw_value="合同签字盖章生效",
                reason="条款明示条件。",
                status="found",
                value="on_signing_and_seal",
            ),
            "trigger_text": experiment.ObjectPropertyValue(
                raw_value="合同签字盖章生效",
                reason="生效原文。",
                status="found",
                value="合同签字盖章生效",
            ),
        }
    )

    normalized = experiment.normalize_effective_date_provenance(
        "effective_mechanism", candidate
    )

    assert normalized.properties["date"].status == "not_found"
    assert normalized.properties["date"].value is None
    assert normalized.properties["date"].raw_value == "2025/9/8"


def test_conditional_effective_date_with_clause_provenance_is_preserved() -> None:
    experiment = load_experiment_module()
    candidate = experiment.ObjectFieldCandidate(
        properties={
            "date": experiment.ObjectPropertyValue(
                raw_value="2025年9月8日",
                reason="条款明示日期。",
                status="found",
                value="2025-09-08",
            ),
            "trigger_type": experiment.ObjectPropertyValue(
                raw_value="双方盖章后于2025年9月8日生效",
                reason="条款明示条件。",
                status="found",
                value="on_signing_and_seal",
            ),
            "trigger_text": experiment.ObjectPropertyValue(
                raw_value="双方盖章后，于2025年9月8日生效",
                reason="生效原文。",
                status="found",
                value="双方盖章后，于2025年9月8日生效",
            ),
        }
    )

    normalized = experiment.normalize_effective_date_provenance(
        "effective_mechanism", candidate
    )

    assert normalized == candidate


def test_signing_effective_date_may_use_unique_signing_date() -> None:
    experiment = load_experiment_module()
    candidate = experiment.ObjectFieldCandidate(
        properties={
            "date": experiment.ObjectPropertyValue(
                raw_value="2025年2月21日",
                reason="唯一签订日期可与签订触发规则组合。",
                status="found",
                value="2025-02-21",
            ),
            "trigger_type": experiment.ObjectPropertyValue(
                raw_value="本合同自甲乙双方签署之日起生效",
                reason="条款明示签订触发。",
                status="found",
                value="on_signing",
            ),
            "trigger_text": experiment.ObjectPropertyValue(
                raw_value="本合同自甲乙双方签署之日起生效",
                reason="生效原文。",
                status="found",
                value="本合同自甲乙双方签署之日起生效",
            ),
        }
    )

    assert experiment.normalize_effective_date_provenance(
        "effective_mechanism", candidate
    ) == candidate


def test_nullable_contract_number_does_not_fail_final_validation() -> None:
    experiment = load_experiment_module()
    missing = experiment.CoreFieldValue(
        raw_value=None,
        reason="已检查可见页面，未找到当前合同编号。",
        status="not_found",
        value=None,
    )

    assert experiment.validate_field_envelope("contract_number", missing) == []
    assert experiment.validate_required_fields({"contract_number": missing}, set()) == []


def test_contract_number_still_accepts_found_non_empty_value() -> None:
    experiment = load_experiment_module()
    found = experiment.CoreFieldValue(
        raw_value="合同编号：HT-001",
        reason="当前合同编号明确。",
        status="found",
        value="HT-001",
    )

    assert experiment.validate_required_fields({"contract_number": found}, set()) == []


def test_application_validation_rejects_not_found_empty_object() -> None:
    experiment = load_experiment_module()
    field = experiment.CoreFieldValue(
        raw_value=None,
        reason="合同未约定整体有效期。",
        status="not_found",
        value={
            "start_date": None,
            "end_date": None,
            "duration_text": None,
            "auto_renewal": None,
        },
    )

    assert experiment.validate_field_envelope(
        "contract_validity_period", field
    ) == ["not_found 状态的 value 必须为 null"]


def test_object_property_statuses_are_validated_and_aggregated() -> None:
    experiment = load_experiment_module()
    properties = {
        "amount": experiment.ObjectPropertyValue(
            raw_value="合计（元）￥2,500.00",
            status="found",
            value=2500,
            reason="合同级总额明确。",
        ),
        "tax_rate": experiment.ObjectPropertyValue(
            raw_value="含税金额与未税金额费用范围不一致",
            status="out_of_scope",
            value=None,
            reason="费用范围不同，不能反推隐含税率。",
        ),
    }
    candidate = experiment.ObjectFieldCandidate(properties=properties)

    assert experiment.validate_extracted_field("contract_amount", candidate) == []
    finalized = experiment.finalize_candidate_field(candidate)
    assert isinstance(finalized, experiment.ObjectFieldValue)
    assert finalized.status == "found"
    assert finalized.properties["tax_rate"].status == "out_of_scope"


def test_out_of_scope_property_requires_raw_value_and_null_value() -> None:
    experiment = load_experiment_module()
    malformed = experiment.ObjectPropertyValue(
        raw_value=None,
        status="out_of_scope",
        value=2175,
        reason="错误示例。",
    )

    assert experiment.validate_property_envelope(
        "tax_rate", malformed
    ) == [
        "tax_rate: out_of_scope 状态的 value 必须为 null",
        "tax_rate: out_of_scope 状态必须保留相关 raw_value",
    ]


def test_not_found_property_can_retain_related_raw_text() -> None:
    experiment = load_experiment_module()
    duration = experiment.ObjectPropertyValue(
        raw_value="本合同自签订之日起生效，有效期内无质量问题不接受退货、换货",
        reason="原文没有给出合同整体有效期的具体时长。",
        status="not_found",
        value=None,
    )

    assert experiment.validate_property_envelope("duration_text", duration) == []


def test_not_found_property_still_rejects_non_null_value() -> None:
    experiment = load_experiment_module()
    malformed = experiment.ObjectPropertyValue(
        raw_value="有效期内",
        reason="错误地把不完整原文当作可采用值。",
        status="not_found",
        value="有效期内",
    )

    assert experiment.validate_property_envelope(
        "duration_text", malformed
    ) == ["duration_text: not_found 状态的 value 必须为 null"]


def test_aggregate_field_metrics_keeps_partial_failures_visible() -> None:
    experiment = load_experiment_module()
    records = [
        {
            "status": "succeeded",
            "metrics": {
                "elapsed_seconds": 1.25,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        },
        {
            "status": "failed",
            "metrics": {
                "elapsed_seconds": 2.5,
                "usage": {"prompt_tokens": 110, "completion_tokens": 40, "total_tokens": 150},
            },
        },
    ]

    metrics = experiment.aggregate_field_metrics(records)

    assert metrics["field_count"] == 2
    assert metrics["successful_field_count"] == 1
    assert metrics["failed_field_count"] == 1
    assert metrics["fields"] == records
    assert metrics["aggregate_elapsed_seconds"] == 3.75
    assert metrics["aggregate_usage"] == {
        "prompt_tokens": 210,
        "completion_tokens": 60,
        "total_tokens": 270,
    }
