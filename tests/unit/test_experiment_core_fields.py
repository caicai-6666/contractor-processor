"""Core 提取实验逐字段调用与前缀布局的回归测试。"""

import importlib.util
from pathlib import Path
import sys

import pytest

from contract_processor.application.schemas.core_extraction import (
    build_core_extraction_schema,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_PATH = PROJECT_ROOT / "experiments/core_field_extraction/run.py"
STEP_1_PROMPT_PATH = (
    PROJECT_ROOT / "experiments/core_field_extraction/prompts/01_understand_contract.txt"
)
STEP_2_PROMPT_PATH = (
    PROJECT_ROOT / "experiments/core_field_extraction/prompts/02_extract_core.txt"
)


def load_experiment_module():
    spec = importlib.util.spec_from_file_location("core_field_extraction_experiment", RUN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Pydantic 解析 future annotations 时需要能从 sys.modules 找到实验模块。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_step2_cli_is_fixed_to_single_field_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    experiment = load_experiment_module()
    monkeypatch.setattr(sys, "argv", ["run.py"])

    args = experiment.parse_args(PROJECT_ROOT)

    assert not hasattr(args, "core_fields_per_batch")
    assert experiment.STEP_2_FIELD_MAX_COMPLETION_TOKENS == 6144


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

    assert "金额与费用穷举" in step1_prompt
    assert "amount_and_fee_mentions 必须覆盖可见页面中的全部金额与费用原文" in step1_prompt
    assert "{{CONTRACT_UNDERSTANDING_BULLETS}}" in step2_prompt
    assert "必须逐条核对“合同理解条目”中的“金额与费用原文清单”" in step2_prompt
    assert "每一个计算输入都必须同时得到该清单和图像支持" in step2_prompt
    assert "不得仅因金额位于产品行就推断为单价" in step1_prompt
    assert "tax_exclusive_price/tax_inclusive_amount" in step1_prompt
    assert "不输出独立未税金额子字段" in step2_prompt
    assert "仍必须保留在 source_amount_text" in step2_prompt


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
    assert "顶层严格按 document_id、fields 的顺序" in prompt
    assert "不得生成根级 reason" in prompt
    assert "每个直属子字段严格按 raw_value、reason、status、value 的顺序" in prompt
    assert "对象外层 status 由程序汇总" in prompt
    assert "out_of_scope" in prompt
    assert "不输出独立未税金额子字段" in prompt
    assert "tax_exclusive_amount" not in prompt
    assert "不得因未税金额不再独立输出而把 contract_amount 或其他明确子字段标为 ambiguous" in prompt


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
