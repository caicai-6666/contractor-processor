"""原 PDF 六栏目合同摘要实验的确定性规则回归测试。"""

import importlib.util
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError
import yaml

from contract_processor.application.prompts.pdf_prefix import (
    build_page_visibility_context,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_PATH = (
    PROJECT_ROOT
    / "src/contract_processor/infrastructure/extraction/abstract/pipeline.py"
)
INITIAL_PROMPT = PROJECT_ROOT / (
    "src/contract_processor/infrastructure/extraction/abstract/prompts/"
    "01_extract_summary_sections.txt"
)
RETRY_PROMPT = PROJECT_ROOT / (
    "src/contract_processor/infrastructure/extraction/abstract/prompts/02_retry_summary_section.txt"
)
POLICY_PATH = PROJECT_ROOT / "data/definitions/contract_summary.yaml"


def load_policy() -> dict[str, object]:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def load_experiment_module():
    spec = importlib.util.spec_from_file_location("contract_summary_experiment", RUN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def found_scalar(experiment, summary: str, evidence: str | None = None):
    return experiment.ScalarSummarySection(
        evidence_text=evidence or summary,
        page_refs=[1],
        reason="原文明确记载。",
        status="found",
        summary_text=summary,
    )


def found_parties(
    experiment, items: list[tuple[str, str]], evidence: str | None = None
):
    return experiment.PartySummarySection(
        evidence_text=evidence or " ".join(f"{role} {name}" for role, name in items),
        page_refs=[1],
        reason="原文明确记载。",
        status="found",
        summary_items=[
            experiment.PartySummaryItem(role=role, name=name)
            for role, name in items
        ],
    )


def found_time(experiment, items: list[tuple[str, str]], evidence: str | None = None):
    return experiment.TimeSummarySection(
        evidence_text=evidence or " ".join(text for _, text in items),
        page_refs=[1],
        reason="原文明确记载。",
        status="found",
        summary_items=[
            experiment.TimeSummaryItem(type=item_type, text=text)
            for item_type, text in items
        ],
    )


def found_performance(
    experiment, items: list[tuple[str, str]], evidence: str | None = None
):
    return experiment.PerformanceSummarySection(
        evidence_text=evidence or " ".join(text for _, text in items),
        page_refs=[1],
        reason="原文明确记载。",
        status="found",
        summary_items=[
            experiment.PerformanceSummaryItem(type=item_type, text=text)
            for item_type, text in items
        ],
    )


def candidate_fixture(experiment):
    return experiment.DirectPdfSummaryCandidate(
        contract_number=found_scalar(
            experiment, "HT-001", "合同编号：HT-001"
        ),
        contract_title=found_scalar(experiment, "设备采购合同"),
        parties=found_parties(experiment, [("买方", "甲公司"), ("卖方", "乙公司")]),
        time=found_time(experiment, [("signing_date", "2026年1月10日")]),
        main_content=found_scalar(
            experiment,
            "甲公司向乙公司采购加热台，合同总额为人民币300000元。",
        ),
        key_performance_terms=found_performance(
            experiment, [("payment", "验收合格后10个工作日内支付70%尾款。")]
        ),
    )


def test_schema_has_fixed_sections_and_evidence_first() -> None:
    experiment = load_experiment_module()

    root_properties = experiment.DirectPdfSummaryCandidate.model_json_schema()[
        "properties"
    ]
    scalar_properties = experiment.ScalarSummarySection.model_json_schema()[
        "properties"
    ]
    party_properties = experiment.PartySummarySection.model_json_schema()["properties"]
    time_properties = experiment.TimeSummarySection.model_json_schema()["properties"]
    performance_properties = experiment.PerformanceSummarySection.model_json_schema()[
        "properties"
    ]

    assert list(root_properties) == list(experiment.SECTION_IDS)
    assert list(scalar_properties) == [
        "evidence_text",
        "page_refs",
        "reason",
        "status",
        "summary_text",
    ]
    assert list(party_properties) == [
        "evidence_text",
        "page_refs",
        "reason",
        "status",
        "summary_items",
    ]
    assert list(time_properties) == [
        "evidence_text",
        "page_refs",
        "reason",
        "status",
        "summary_items",
    ]
    assert list(performance_properties) == list(party_properties)
    party_item_schema = experiment.PartySummaryItem.model_json_schema()
    assert list(party_item_schema["properties"]) == ["role", "name"]
    time_item_schema = experiment.TimeSummaryItem.model_json_schema()
    assert list(time_item_schema["properties"]) == ["type", "text"]
    assert time_item_schema["properties"]["type"]["enum"] == [
        "signing_date",
        "effective_date",
        "effective_info",
        "validity_period",
    ]
    performance_item_schema = experiment.PerformanceSummaryItem.model_json_schema()
    assert list(performance_item_schema["properties"]) == ["type", "text"]
    assert performance_item_schema["properties"]["type"]["enum"] == [
        "payment",
        "delivery_service",
        "acceptance",
        "quality_warranty",
        "breach_termination",
        "dispute_resolution",
    ]
    all_properties = (
        scalar_properties
        | party_properties
        | time_properties
        | performance_properties
    )
    assert "priority" not in all_properties
    assert "decision" not in all_properties


def test_messages_place_all_pdf_images_before_task_suffix() -> None:
    experiment = load_experiment_module()
    images = [
        {"data_url": "data:image/png;base64,AAA"},
        {"data_url": "data:image/png;base64,BBB"},
    ]

    messages = experiment.messages_for("共同前缀", images, "本次任务")
    content = messages[1]["content"]

    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "image_url",
        "text",
    ]
    assert content[0]["text"] == "共同前缀"
    assert content[-1]["text"] == "本次任务"


def test_page_visibility_states_whole_pdf_and_physical_pages() -> None:
    context = build_page_visibility_context(5)

    assert "共 5 个物理页" in context
    assert "全部页面：1、2、3、4、5" in context
    assert "不得根据合同常见结构、文件名或外部知识补写" in context


def test_found_section_passes_business_validation() -> None:
    experiment = load_experiment_module()
    section = found_scalar(experiment, "HT-001", "合同编号： HT-001")

    errors = experiment.validate_section(
        "contract_number", section, 2, {"required": False}
    )

    assert errors == []


def test_contract_number_must_be_verbatim_from_evidence() -> None:
    experiment = load_experiment_module()
    section = found_scalar(experiment, "HT-002", "合同编号：HT-001")

    errors = experiment.validate_section(
        "contract_number", section, 2, {"required": False}
    )

    assert {error.code for error in errors} == {"CONTRACT_NUMBER_NOT_VERBATIM"}


def test_contract_number_may_be_null_without_validation_failure() -> None:
    experiment = load_experiment_module()
    section = experiment.ScalarSummarySection(
        evidence_text=None,
        page_refs=[],
        reason="可见页面没有可靠合同编号。",
        status="not_found",
        summary_text=None,
    )

    errors = experiment.validate_section(
        "contract_number", section, 2, {"required": False}
    )

    assert errors == []


def test_non_found_allows_null_summary_and_matching_evidence_pair() -> None:
    experiment = load_experiment_module()
    section = experiment.TimeSummarySection(
        evidence_text=None,
        page_refs=[],
        reason="没有可靠合同级时间事实。",
        status="not_found",
        summary_items=None,
    )

    assert experiment.validate_section("time", section, 2, {"required": False}) == []


def test_numeric_difference_is_left_to_evidence_and_human_audit() -> None:
    experiment = load_experiment_module()
    section = found_performance(
        experiment,
        [("payment", "验收后20个工作日内支付70%。")],
        "验收后10个工作日内支付70%。",
    )

    errors = experiment.validate_section(
        "key_performance_terms", section, 2, {"required": False}
    )

    assert errors == []


def test_parties_and_time_reject_sensitive_details() -> None:
    experiment = load_experiment_module()
    parties = found_parties(experiment, [("法定代表人", "张三")])
    time = found_time(experiment, [("effective_info", "开户行：某银行")])

    party_errors = experiment.validate_section(
        "parties", parties, 2, {"required": False}
    )
    time_errors = experiment.validate_section("time", time, 2, {"required": False})

    assert "PARTY_CONTAINS_NON_PARTY_DETAILS" in {
        error.code for error in party_errors
    }
    assert "TIME_CONTAINS_NON_TIME_DETAILS" in {
        error.code for error in time_errors
    }


def test_time_schema_rejects_non_time_item_type() -> None:
    experiment = load_experiment_module()

    with pytest.raises(ValidationError):
        experiment.TimeSummarySection.model_validate(
            {
                "evidence_text": "合同签订后10日内支付。",
                "page_refs": [1],
                "reason": "付款期限不属于合同级时间。",
                "status": "found",
                "summary_items": [
                    {"type": "payment", "text": "合同签订后10日内支付。"}
                ],
            }
        )


def test_time_text_rejects_repeated_display_label() -> None:
    experiment = load_experiment_module()
    section = found_time(
        experiment,
        [("signing_date", "签订日期：2025年8月12日")],
    )

    errors = experiment.validate_section("time", section, 2, {"required": False})

    assert {error.code for error in errors} == {"TIME_TEXT_REPEATS_LABEL"}


def test_structured_items_are_rendered_with_program_owned_labels() -> None:
    experiment = load_experiment_module()
    policy = load_policy()
    candidate = candidate_fixture(experiment).model_copy(
        update={
            "time": found_time(
                experiment,
                [
                    ("signing_date", "2026年1月10日"),
                    ("effective_info", "双方签字盖章后生效"),
                ],
            )
        }
    )

    summary = experiment.render_final_summary(candidate, policy)

    assert "【相关方】\n买方：甲公司\n卖方：乙公司" in summary
    assert "【时间】\n签订日期：2026年1月10日\n生效信息：双方签字盖章后生效" in summary
    assert "【关键履约约定】\n付款：验收合格后10个工作日内支付70%尾款。" in summary


def test_performance_terms_require_types_in_semantic_order() -> None:
    experiment = load_experiment_module()
    section = found_performance(
        experiment,
        [
            ("acceptance", "到货后3日内验收。"),
            ("payment", "验收后10日内付款。"),
        ],
    )

    errors = experiment.validate_section(
        "key_performance_terms", section, 2, {"required": False}
    )

    assert "PERFORMANCE_TYPE_ORDER_INVALID" in {error.code for error in errors}


def test_party_schema_rejects_legacy_labeled_strings() -> None:
    experiment = load_experiment_module()

    with pytest.raises(ValidationError):
        experiment.PartySummarySection.model_validate(
            {
                "evidence_text": "买方：甲公司",
                "page_refs": [1],
                "reason": "原文明确记载。",
                "status": "found",
                "summary_items": ["买方：甲公司"],
            }
        )


def test_performance_schema_rejects_legacy_labeled_strings() -> None:
    experiment = load_experiment_module()

    with pytest.raises(ValidationError):
        experiment.PerformanceSummarySection.model_validate(
            {
                "evidence_text": "验收后付款。",
                "page_refs": [1],
                "reason": "原文明确记载。",
                "status": "found",
                "summary_items": ["付款：验收后付款。"],
            }
        )


def test_performance_terms_reject_duplicate_types() -> None:
    experiment = load_experiment_module()
    section = found_performance(
        experiment,
        [("payment", "支付预付款。"), ("payment", "支付尾款。")],
    )

    errors = experiment.validate_section(
        "key_performance_terms", section, 2, {"required": False}
    )

    assert "PERFORMANCE_TYPE_DUPLICATED" in {error.code for error in errors}


def test_performance_terms_reject_missing_fact_placeholders() -> None:
    experiment = load_experiment_module()
    section = found_performance(
        experiment,
        [("acceptance", "无明确验收条款")],
        evidence="合同其他履约条款",
    )

    errors = experiment.validate_section(
        "key_performance_terms", section, 2, {"required": False}
    )

    assert "SUMMARY_CONTAINS_PLACEHOLDER" in {error.code for error in errors}
    assert any("必须从 summary_items 中删除" in error.retry_guidance for error in errors)


def test_page_refs_must_be_sorted_unique_and_visible() -> None:
    experiment = load_experiment_module()

    errors = experiment.validate_page_refs([2, 2, 4, 1], 3, "time")

    assert [error.code for error in errors] == [
        "PAGE_REFS_NOT_SORTED",
        "PAGE_REFS_DUPLICATED",
        "PAGE_REFS_OUT_OF_RANGE",
    ]
    assert errors[-1].internal_message == "time: page_refs 包含不可见页码 [4]"


def test_retry_feedback_is_semantic_and_hides_internal_message() -> None:
    experiment = load_experiment_module()
    issue = experiment.validation_issue(
        "PARTY_DUPLICATED",
        "parties: summary_items 不得包含重复主体",
        "你重复输出了相同角色和主体。请按角色与主体去重。",
    )

    feedback = experiment.render_retry_feedback([issue])
    serialized = experiment.validation_issues_to_json({"parties": [issue]})

    assert feedback == "- 问题 1：你重复输出了相同角色和主体。请按角色与主体去重。"
    assert "summary_items" not in feedback
    assert "PARTY_DUPLICATED" not in feedback
    assert serialized["parties"][0]["code"] == "PARTY_DUPLICATED"


def test_final_summary_uses_fixed_order_and_keeps_empty_section() -> None:
    experiment = load_experiment_module()
    policy = load_policy()
    candidate = candidate_fixture(experiment)
    candidate = candidate.model_copy(
        update={
            "time": experiment.TimeSummarySection(
                evidence_text=None,
                page_refs=[],
                reason="没有可靠合同级时间事实。",
                status="not_found",
                summary_items=None,
            )
        }
    )

    summary = experiment.render_final_summary(candidate, policy)

    assert summary.startswith("【合同编号】\nHT-001\n\n【合同标题】")
    assert "【时间】\n\n【主要内容】" in summary
    assert summary.index("【相关方】") < summary.index("【时间】")
    assert summary.index("【主要内容】") < summary.index("【关键履约约定】")


def test_prompts_directly_read_pdf_and_retry_only_one_section() -> None:
    initial = INITIAL_PROMPT.read_text(encoding="utf-8")
    retry = RETRY_PROMPT.read_text(encoding="utf-8")

    assert "直接阅读原始合同 PDF" in initial
    assert "一次性提取固定摘要的六个栏目" in initial
    assert "先定位 PDF 图像中可见的最小必要原文" in initial
    assert "`{role, name}`" in initial + retry
    assert "`{type, text}`" in initial
    assert "signing_date" in initial + retry
    assert "delivery_service" in initial
    assert "展示标签和冒号均由程序生成" in retry
    assert "Core" not in initial
    assert "Clause" not in initial
    assert "priority" not in initial + retry
    assert "上一次未通过程序校验的唯一摘要栏目" in retry
    assert "{{SECTION_INSTRUCTION}}" in retry
    assert "{{VALIDATION_FEEDBACK}}" in retry
    assert "不是合同事实来源" in retry


def test_policy_declares_direct_pdf_mode_and_one_retry() -> None:
    experiment = load_experiment_module()
    policy = load_policy()

    assert policy["mode"] == "direct_pdf"
    assert policy["schema_version"] == "0.8"
    assert "max_summary_characters" not in policy
    assert policy["sections"]["contract_number"]["required"] is False
    assert policy["max_retries_per_section"] == 1
    assert list(policy["sections"]) == list(experiment.SECTION_IDS)
    assert policy["sections"]["parties"]["output_mode"] == "party_items"
    assert policy["sections"]["time"]["output_mode"] == "time_items"
    assert (
        policy["sections"]["key_performance_terms"]["output_mode"]
        == "performance_items"
    )
