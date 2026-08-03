"""固定 Attribute 的跨合同语义边界与局部重试回归。"""

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from contract_processor.infrastructure.extraction.attribute import pipeline
from contract_processor.infrastructure.extraction.field_values import (
    FieldExtractionCandidate,
    ObjectFieldValue,
    ObjectPropertyValue,
    ScalarFieldValue,
)
from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ATTRIBUTE_YAML = PROJECT_ROOT / "data/definitions/attribute.yaml"
CORE_YAML = PROJECT_ROOT / "data/definitions/core.yaml"
DOCUMENT_ID = "a" * 64


def scalar(*, raw_value: str | None, status: str, value: Any) -> ScalarFieldValue:
    return ScalarFieldValue(
        raw_value=raw_value,
        reason="测试字段的直接原文依据。",
        status=status,  # type: ignore[arg-type]
        value=value,
    )


def property_value(
    *, raw_value: str | None, status: str, value: Any
) -> ObjectPropertyValue:
    return ObjectPropertyValue(
        raw_value=raw_value,
        reason="测试子字段的直接原文依据。",
        status=status,  # type: ignore[arg-type]
        value=value,
    )


def test_semantic_validator_rejects_project_name_as_project_number() -> None:
    errors = pipeline.validate_attribute_business_rules(
        "project_numbers",
        scalar(raw_value="XYZZ+RJ28+EJS42", status="found", value=["XYZZ+RJ28+EJS42"]),
    )

    assert errors == [
        "project_numbers: found 的 raw_value 必须包含明确项目编号语义的标签，"
        "项目名称或其中的编码式片段不能单独采用"
    ]


def test_semantic_validator_requires_payment_event_and_deadline_to_be_separate() -> None:
    errors = pipeline.validate_attribute_business_rules(
        "payment_schedule",
        scalar(
            raw_value="合同签订后3个工作日内支付50%预付款。",
            status="found",
            value=[
                {
                    "stage_name": "预付款",
                    "trigger_text": "合同签订后3个工作日内",
                    "ratio": 50,
                    "amount": 55000,
                    "currency": "CNY",
                    "due_text": "合同签订后3个工作日内",
                },
                {
                    "stage_name": "发货款",
                    "trigger_text": "卖方具备交付产品时",
                    "ratio": 40,
                    "amount": 44000,
                    "currency": "CNY",
                    "due_text": "卖方具备交付产品时",
                },
            ],
        ),
    )

    assert len(errors) == 3
    assert "payment_schedule[1]: trigger_text 与 due_text 不能复制" in errors[0]
    assert "payment_schedule[2]: trigger_text 与 due_text 不能复制" in errors[1]
    assert "payment_schedule[2]: due_text 必须是付款期限" in errors[2]


def test_semantic_validator_rejects_payment_deadline_as_acceptance_deadline() -> None:
    field = ObjectFieldValue(
        status="found",
        properties={
            "standard_text": property_value(raw_value=None, status="not_found", value=None),
            "deadline_text": property_value(
                raw_value="在货物出厂之日起2个月内支付验收款10%。",
                status="found",
                value="在货物出厂之日起2个月内支付验收款10%。",
            ),
            "deemed_accepted": property_value(
                raw_value=None, status="not_found", value=None
            ),
        },
    )

    errors = pipeline.validate_attribute_business_rules("acceptance_mechanism", field)

    assert errors == [
        "acceptance_mechanism.deadline_text: 仅包含付款义务或验收款支付期限，"
        "未体现验收行为、标准、异议或验收期限，不能作为验收期限"
    ]


def test_semantic_validator_rejects_relational_court_as_institution_name() -> None:
    field = ObjectFieldValue(
        status="found",
        properties={
            "mechanism": property_value(
                raw_value="应提交买方当地人民法院解决",
                status="found",
                value="litigation",
            ),
            "institution_name": property_value(
                raw_value="买方当地人民法院",
                status="found",
                value="买方当地人民法院",
            ),
            "jurisdiction_text": property_value(
                raw_value="应提交买方当地人民法院解决",
                status="found",
                value="应提交买方当地人民法院解决",
            ),
        },
    )

    errors = pipeline.validate_attribute_business_rules("dispute_resolution", field)

    assert errors == [
        "dispute_resolution.institution_name: 关系性地域法院描述不能作为具体机构名称，"
        "应为 null 并保留在 jurisdiction_text"
    ]


def test_attribute_retries_a_field_after_semantic_validation_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """重试仅注入校验指导，第二次成功结果替代第一次错误候选。"""

    catalog_payload = yaml.safe_load(ATTRIBUTE_YAML.read_text(encoding="utf-8"))
    project_field = next(
        field
        for field in catalog_payload["fields"]
        if field["field_id"] == "project_numbers"
    )
    temporary_catalog = {
        "schema_version": "test",
        "field_set": "attribute",
        "status": "draft",
        "extraction": {"max_retries_per_field": 1},
        "fields": [deepcopy(project_field)],
    }
    (tmp_path / "attribute.yaml").write_text(
        yaml.safe_dump(temporary_catalog, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    def fake_settings(_: Path) -> dict[str, Any]:
        return {
            "models": {
                "mllm": {
                    "context_window_tokens": 1024,
                    "model": "fake-model",
                    "generation": {"max_completion_tokens": 64},
                }
            },
            "paths": {"attribute_fields": "attribute.yaml", "core_fields": str(CORE_YAML)},
        }

    prompts: list[str] = []
    results = iter(
        [
            FieldExtractionCandidate.model_validate(
                {
                    "fields": {
                        "project_numbers": {
                            "raw_value": "XYZZ+RJ28+EJS42",
                            "reason": "项目名称中存在编码式片段。",
                            "status": "found",
                            "value": ["XYZZ+RJ28+EJS42"],
                        }
                    }
                }
            ),
            FieldExtractionCandidate.model_validate(
                {
                    "fields": {
                        "project_numbers": {
                            "raw_value": "项目名称：XYZZ+RJ28+EJS42 机械臂",
                            "reason": "只有项目名称，未出现明确项目编号标签。",
                            "status": "ambiguous",
                            "value": None,
                        }
                    }
                }
            ),
        ]
    )

    async def fake_invoke_json(**kwargs: Any):
        prompts.append(kwargs["prompt_suffix"])
        return next(results), {"elapsed_seconds": 0.01, "usage": {}}

    async def fake_common_prefix(_: int) -> str:
        return "公共前缀"

    monkeypatch.setattr(pipeline, "_load_settings_sync", fake_settings)
    monkeypatch.setattr(pipeline, "invoke_json", fake_invoke_json)
    monkeypatch.setattr(pipeline, "build_common_prefix", fake_common_prefix)

    result = asyncio.run(
        pipeline.run_attribute_extraction(
            project_root_path=tmp_path,
            document_id=DOCUMENT_ID,
            shared_images=[
                {"page": 1, "data_url": "data:image/png;base64,AA==", "image_bytes": 1}
            ],
            shared_source_page_count=1,
            shared_client=object(),  # type: ignore[arg-type]
            model_request_limiter=ModelRequestLimiter(1),
            core_fields={},
            contract_understanding_bullets="- 第 1 页：项目名称",
        )
    )

    assert len(prompts) == 2
    assert "本次局部重试的校验反馈" not in prompts[0]
    assert "项目名称或其中的编码式片段不能单独采用" in prompts[1]
    assert result.payload[0]["status"] == "ambiguous"
    assert result.payload[0]["value"] is None
    assert result.metrics["fields"][0]["attempt_count"] == 2
