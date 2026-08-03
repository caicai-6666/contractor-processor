"""字段发现第一阶段实验的无模型回归测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.field_discovery_stage_one.discovery import (
    CandidateEvidence,
    CandidateMatch,
    CandidateProposal,
    CandidateSemanticGateBatch,
    CandidateSemanticGateDecision,
    CandidateVectorPool,
    FIELD_RELATION_SYSTEM_MESSAGE,
    finalize_relation_reason,
    RelationComparison,
    RelationJudgement,
    SingleRelationJudgement,
    _messages,
    build_discovery_prompt_after_images,
    build_discovery_prompt_before_images,
    build_candidate_proposal_repair_prompt,
    build_single_candidate_semantic_gate_prompt,
    build_extraction_rule_revision_prompt,
    build_relation_prompt,
    resolve_candidate_identity,
    parse_candidate_proposal_batch_payload,
    validate_candidate_proposal,
    validate_candidate_semantic_gate,
    validate_extraction_rule_revision,
    validate_generalized_extraction_rule,
    validate_relation_judgement,
    validate_single_relation_semantics,
)
from experiments.field_discovery_stage_one.field_description import (
    OutputDescription,
    compile_output_description,
    render_field_card,
)
from experiments.field_discovery_stage_one.run import build_ide_argv, parse_args


class FakeEmbeddingClient:
    """稳定的本地替身：只验证内存候选池的调用与身份规则。"""

    async def embed_field_summary(self, summary: str) -> list[float]:
        # 不追求语义质量；只需要非零、固定维度向量以覆盖 LlamaIndex 内存索引路径。
        seed = sum(ord(character) for character in summary)
        return [float(seed % 17 + 1), float(seed % 29 + 1), 1.0]


def proposal(*, field_id: str, name: str) -> CandidateProposal:
    return CandidateProposal(
        field_id=field_id,
        name=name,
        meaning=f"{name}在合同履行过程中的稳定业务含义。",
        output=OutputDescription(type="string"),
        extraction_rule=f"仅在合同明确约定{name}时提取原文。",
        novelty_reason=f"该字段不是现有固定字段{name}的同义表达。",
        status="accepted",
        evidence=CandidateEvidence(page_number=1, source_text="合同中存在足以证明该字段的明确约定。"),
    )


def record(*, field_id: str, name: str):
    return validate_candidate_proposal(
        proposal(field_id=field_id, name=name),
        fixed_definitions=(),
        source_page_count=1,
    )


def test_candidate_gate_rejects_exact_fixed_field_alias() -> None:
    fixed = replace(
        record(field_id="payment_schedule", name="付款安排").definition,
        aliases=("付款计划",),
    )

    with pytest.raises(ValueError, match="精确重合"):
        validate_candidate_proposal(
            proposal(field_id="payment_terms", name="付款计划"),
            fixed_definitions=(fixed,),
            source_page_count=1,
        )


def test_semantic_gate_requires_exact_batch_coverage_and_valid_fixed_reference() -> None:
    fixed = record(field_id="delivery_locations", name="交付地点").definition
    valid = CandidateSemanticGateBatch(
        decisions=[
            CandidateSemanticGateDecision(
                proposal_index=1,
                covered_by_field_id="delivery_locations",
                reason="候选整体语义已经由固定交付地点字段覆盖，因此 status=covered_by_fixed",
                status="covered_by_fixed",
            )
        ]
    )

    decisions = validate_candidate_semantic_gate(
        valid, expected_indices=(1,), fixed_definitions=(fixed,)
    )

    assert decisions[1].status == "covered_by_fixed"
    with pytest.raises(ValueError, match="不属于固定目录"):
        validate_candidate_semantic_gate(
            valid.model_copy(
                update={
                    "decisions": [
                        valid.decisions[0].model_copy(
                            update={"covered_by_field_id": "unknown_field"}
                        )
                    ]
                }
            ),
            expected_indices=(1,),
            fixed_definitions=(fixed,),
        )


def test_candidate_schema_omits_unavailable_first_stage_governance_fields() -> None:
    properties = CandidateProposal.model_json_schema()["properties"]

    assert "aliases" not in properties
    assert "not_meaning" not in properties
    assert "examples" not in properties
    assert properties["output"] == {"$ref": "#/$defs/OutputDescription"}


def test_candidate_batch_parser_keeps_valid_candidates_when_one_candidate_is_invalid() -> None:
    valid_payload = proposal(field_id="warranty_period", name="保修期").model_dump(
        mode="json"
    )
    invalid_payload = proposal(field_id="payment_terms", name="付款安排").model_dump(
        mode="json"
    )
    invalid_payload["output"] = {"type": "string", "items": {"name": "非法"}}

    valid, failures = parse_candidate_proposal_batch_payload(
        json.dumps({"candidates": [valid_payload, invalid_payload]})
    )

    assert [(index, item.field_id) for index, item in valid] == [(1, "warranty_period")]
    assert len(failures) == 1
    assert failures[0].proposal_index == 2
    assert failures[0].payload == invalid_payload
    assert "items" in failures[0].error


def test_candidate_repair_prompt_targets_one_candidate_without_pdf_regeneration() -> None:
    prompt = build_candidate_proposal_repair_prompt(
        proposal_payload={"field_id": "payment_terms", "name": "付款安排"},
        validation_error="output 不允许 items",
    )

    assert "只输出这一个候选对象" in prompt
    assert "不要输出 candidates 包络" in prompt
    assert "不重读 PDF" in build_candidate_proposal_repair_prompt.__doc__
    assert "output 不允许 items" in prompt


def test_single_candidate_semantic_gate_prompt_binds_one_candidate_index() -> None:
    prompt = build_single_candidate_semantic_gate_prompt(
        candidate_index=3,
        candidate=record(field_id="warranty_period", name="保修期"),
        fixed_definitions=(),
    )

    assert "proposal_index 必须固定输出为 3" in prompt
    assert "一个刚生成的候选字段" in prompt
    assert "只输出一个候选语义准入 JSON 对象" in prompt


def test_output_type_description_is_compiled_into_canonical_recursive_definition() -> None:
    description = OutputDescription.model_validate(
        {
            "type": "array",
            "min_items": 1,
            "items": {
                "name": "付款阶段",
                "meaning": "合同明确约定的单个付款阶段。",
                "extraction_rule": "仅保留合同明确约定的付款阶段，不补造阶段。",
                "output": {
                    "type": "object",
                    "properties": [
                        {
                            "field_id": "stage_name",
                            "name": "阶段名称",
                            "meaning": "预付款、发货款或尾款等阶段称谓。",
                            "output": {"type": "string"},
                            "extraction_rule": "忠实保留合同明确写明的付款阶段名称。",
                        },
                        {
                            "field_id": "ratio",
                            "name": "付款比例",
                            "meaning": "当前付款阶段占计价基数的百分比。",
                            "output": {"type": "number", "unit": "percent"},
                            "extraction_rule": "只提取合同明示比例，不根据金额反推。",
                        },
                    ],
                },
            },
        }
    )

    compiled = compile_output_description(description)

    assert compiled["nullable"] is True
    assert compiled["min_items"] == 1
    assert compiled["items"]["nullable"] is False
    assert compiled["items"]["additional_properties"] is False
    assert compiled["items"]["required"] == ["stage_name", "ratio"]
    assert compiled["items"]["properties"]["stage_name"]["nullable"] is True
    assert compiled["items"]["properties"]["ratio"]["unit"] == "percent"


def test_output_description_rejects_json_schema_keywords_from_model() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        OutputDescription.model_validate(
            {"type": "string", "nullable": True, "additional_properties": False}
        )


def test_output_description_json_schema_is_discriminated_by_type() -> None:
    schema = OutputDescription.model_json_schema()
    output_schema = schema["$defs"]["OutputDescription"]
    variants = {
        item["properties"]["type"]["const"]: item
        for item in output_schema["oneOf"]
    }

    assert set(variants) == {
        "string",
        "number",
        "integer",
        "boolean",
        "date",
        "enum",
        "object",
        "array",
    }
    assert "items" not in variants["string"]["properties"]
    assert "values" not in variants["string"]["properties"]
    assert variants["array"]["required"] == ["type", "items"]
    assert variants["object"]["required"] == ["type", "properties"]


def test_output_compiler_rejects_parameters_that_do_not_belong_to_selected_type() -> None:
    with pytest.raises(ValueError, match="不允许参数"):
        compile_output_description(OutputDescription(type="string", unit="percent"))

    with pytest.raises(ValueError, match="不允许参数"):
        compile_output_description(
            OutputDescription.model_validate(
                {
                    "type": "object",
                    "unit": "percent",
                    "properties": [
                        {
                            "field_id": "value",
                            "name": "字段值",
                            "meaning": "合同明确约定的业务字段值。",
                            "output": {"type": "string"},
                            "extraction_rule": "仅提取合同明确约定的业务字段值。",
                        }
                    ],
                }
            )
        )


def test_semantic_field_card_omits_empty_governance_lists_and_examples() -> None:
    definition = {
        "field_id": "delivery_method",
        "name": "交货方式",
        "meaning": "合同明确约定的货物运输或配送方式。",
        "aliases": [],
        "not_meaning": [],
        "output": {"type": "string", "nullable": True},
        "extraction_rule": "仅提取合同明确约定的交货方式。",
        "examples": [{"source_text": "不应展示", "output": "物流"}],
    }

    card = render_field_card(definition)

    assert "字段：delivery_method｜交货方式" in card
    assert "常见称谓" not in card
    assert "明确排除" not in card
    assert "examples" not in card
    assert "不应展示" not in card


@pytest.mark.parametrize(
    "rule",
    [
        "从条款7'其它约定'中提取发票类型描述。",
        "从第七条其他约定中提取发票类型描述。",
        "根据第3页表格识别发票类型并输出。",
        "从合同附加信息中提取合同生效日期。",
        "从‘其他约定’中提取发票类型。",
    ],
)
def test_candidate_gate_rejects_document_specific_extraction_locations(rule: str) -> None:
    candidate = proposal(field_id="invoice_type", name="发票类型").model_copy(
        update={"extraction_rule": rule}
    )

    with pytest.raises(ValueError, match="泛化性不足"):
        validate_candidate_proposal(
            candidate,
            fixed_definitions=(),
            source_page_count=3,
        )


def test_generalized_extraction_rule_preserves_business_numbers_without_location_leakage() -> None:
    rule = (
        "仅提取合同明确约定且与开票义务直接关联的发票种类；不得仅凭13%税率推断，"
        "存在多个种类时完整保留，未明确则返回空值。"
    )

    assert validate_generalized_extraction_rule(rule) == rule


def test_candidate_gate_also_rejects_location_leakage_in_object_child_rule() -> None:
    candidate = proposal(field_id="invoice_requirement", name="发票要求").model_copy(
        update={
            "output": OutputDescription.model_validate(
                {
                    "type": "object",
                    "properties": [
                        {
                            "field_id": "invoice_type",
                            "name": "发票类型",
                            "meaning": "合同明确要求开具的发票业务种类。",
                            "output": {"type": "string"},
                            "extraction_rule": "从第七条其他约定中提取发票类型。",
                        }
                    ],
                }
            )
        }
    )

    with pytest.raises(ValueError, match="output.properties.invoice_type.extraction_rule"):
        validate_candidate_proposal(
            candidate,
            fixed_definitions=(),
            source_page_count=3,
        )


def test_rule_revision_prompt_separates_evidence_from_catalog_rule() -> None:
    candidate = proposal(field_id="invoice_type", name="发票类型").model_copy(
        update={"extraction_rule": "从条款7中提取发票类型描述。"}
    )

    prompt = build_extraction_rule_revision_prompt(
        proposal=candidate,
        validation_error="extraction_rule 泛化性不足",
    )

    assert "跨合同字段规范" in prompt
    assert "不得修改字段身份" in prompt
    assert "页码、条款号" in prompt
    assert "只输出 extraction_rule 和完整 output" in prompt


def test_rule_revision_may_change_nested_rules_but_not_output_structure() -> None:
    original = OutputDescription.model_validate(
        {
            "type": "object",
            "properties": [
                {
                    "field_id": "invoice_type",
                    "name": "发票类型",
                    "meaning": "合同明确要求开具的发票业务种类。",
                    "output": {"type": "string"},
                    "extraction_rule": "从第七条中提取发票类型。",
                }
            ],
        }
    )
    revised = OutputDescription.model_validate(
        {
            "type": "object",
            "properties": [
                {
                    "field_id": "invoice_type",
                    "name": "发票类型",
                    "meaning": "合同明确要求开具的发票业务种类。",
                    "output": {"type": "string"},
                    "extraction_rule": "仅提取与开票义务直接关联且合同明确约定的发票类型。",
                }
            ],
        }
    )

    assert (
        validate_extraction_rule_revision(
            original_output=original,
            revised_output=revised,
        )
        == revised
    )

    changed_type = OutputDescription.model_validate(
        {
            "type": "object",
            "properties": [
                {
                    "field_id": "invoice_type",
                    "name": "发票类型",
                    "meaning": "合同明确要求开具的发票业务种类。",
                    "output": {
                        "type": "enum",
                        "values": [
                            {"value": "vat_special", "meaning": "增值税专用发票"}
                        ],
                    },
                    "extraction_rule": "仅提取合同明确约定的发票类型。",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="不得修改 output"):
        validate_extraction_rule_revision(
            original_output=original,
            revised_output=changed_type,
        )


def test_relation_judgement_must_cover_each_retrieved_candidate_once() -> None:
    matches = (
        CandidateMatch("candidate_0001", "group_0001", 0.1, 1),
        CandidateMatch("candidate_0002", "group_0002", 0.09, 2),
    )
    judgement = RelationJudgement(
        comparisons=[
            RelationComparison(
                target_candidate_id="candidate_0001", relation="same", reason="同一概念"
            )
        ]
    )

    with pytest.raises(ValueError, match="必须覆盖全部"):
        validate_relation_judgement(judgement, matches)


def test_relation_reason_is_normalised_to_a_fixed_matching_conclusion() -> None:
    assert (
        finalize_relation_reason(
            reason="两个字段分别记录交付前提与迟延后的责任后果。",
            relation="unrelated",
        )
        == "两个字段分别记录交付前提与迟延后的责任后果。因此 relation=unrelated"
    )
    assert (
        finalize_relation_reason(
            reason="两个字段边界不同。因此 relation=related_distinct。",
            relation="related_distinct",
        )
        == "两个字段边界不同。因此 relation=related_distinct"
    )
    with pytest.raises(ValueError, match="不一致"):
        finalize_relation_reason(
            reason="两个字段相同。因此 relation=same",
            relation="unrelated",
        )


def test_identity_resolution_prioritises_same_over_related_distinct() -> None:
    async def scenario() -> dict[str, Any]:
        pool = CandidateVectorPool(FakeEmbeddingClient())  # type: ignore[arg-type]
        first = await pool.create_identity(
            record(field_id="delivery_window", name="交付期限"), document_id="a" * 64
        )
        second = await pool.create_identity(
            record(field_id="warranty_window", name="质保期限"), document_id="b" * 64
        )
        matches = (
            CandidateMatch(first.candidate_id, first.group_id, 0.10, 1),
            CandidateMatch(second.candidate_id, second.group_id, 0.20, 1),
        )
        resolution = await resolve_candidate_identity(
            proposal=record(field_id="delivery_term", name="交货期限"),
            document_id="c" * 64,
            matches=matches,
            comparisons={
                first.candidate_id: RelationComparison(
                    target_candidate_id=first.candidate_id,
                    relation="same",
                    reason="同一交付期限概念。",
                ),
                second.candidate_id: RelationComparison(
                    target_candidate_id=second.candidate_id,
                    relation="related_distinct",
                    reason="都是期限但业务边界不同。",
                ),
            },
            pool=pool,
        )
        assert pool.identity(first.candidate_id).occurrence_count == 2
        assert pool.size == 2
        return resolution

    result = asyncio.run(scenario())

    assert result["action"] == "reuse_identity"
    assert result["candidate_id"] == "candidate_0001"


def test_related_edges_merge_all_reached_groups_into_one_governance_component() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pool = CandidateVectorPool(FakeEmbeddingClient())  # type: ignore[arg-type]
        payment = await pool.create_identity(
            record(field_id="payment_schedule", name="付款安排"), document_id="a" * 64
        )
        deadline = await pool.create_identity(
            record(field_id="payment_deadline", name="付款期限"), document_id="b" * 64
        )
        matches = (
            CandidateMatch(payment.candidate_id, payment.group_id, 0.02, 1),
            CandidateMatch(deadline.candidate_id, deadline.group_id, 0.01, 2),
        )
        current = record(field_id="prepayment_ratio", name="预付款比例")
        await resolve_candidate_identity(
            proposal=current,
            document_id="c" * 64,
            matches=matches,
            comparisons={
                match.candidate_id: RelationComparison(
                    target_candidate_id=match.candidate_id,
                    reason="属于付款语义族但记录不同事实。",
                    relation="related_distinct",
                )
                for match in matches
            },
            pool=pool,
        )
        return pool.report(), pool.relation_graph_report()

    identities, graph = asyncio.run(scenario())

    assert {item["group_id"] for item in identities} == {"group_0001"}
    assert len(graph["edges"]) == 2
    assert graph["components"][0]["candidate_ids"] == [
        "candidate_0001",
        "candidate_0002",
        "candidate_0003",
    ]


def test_same_rejects_scalar_to_broad_object_top_level_mismatch() -> None:
    scalar = record(field_id="warranty_period", name="质保期限")
    broad_proposal = proposal(field_id="goods_condition", name="货物状态").model_copy(
        update={
            "output": OutputDescription.model_validate(
                {
                    "type": "object",
                    "properties": [
                        {
                            "field_id": "warranty_period",
                            "name": "质保期限",
                            "meaning": "合同明确约定的质保持续期间。",
                            "output": {"type": "string"},
                            "extraction_rule": "仅提取合同明确约定的质保持续期间。",
                        }
                    ],
                }
            )
        }
    )
    broad = validate_candidate_proposal(
        broad_proposal, fixed_definitions=(), source_page_count=1
    )

    with pytest.raises(ValueError, match="顶层字段完整一一对应"):
        validate_single_relation_semantics(
            proposal=scalar,
            target=broad,
            judgement=SingleRelationJudgement(reason="质保期限相同。", relation="same"),
        )


def test_exact_generated_identity_cannot_be_split_only_by_output_representation() -> None:
    first = record(field_id="delivery_method", name="交付方式")
    second = record(field_id="delivery_method", name="交付方式")

    with pytest.raises(ValueError, match="输出表示差异不能"):
        validate_single_relation_semantics(
            proposal=first,
            target=second,
            judgement=SingleRelationJudgement(
                reason="一个保留原文，一个用于分类。", relation="related_distinct"
            ),
        )


def test_multimodal_message_places_variable_task_after_images() -> None:
    context = type(
        "Context",
        (),
        {"images": [{"data_url": "data:image/png;base64,AA=="}]},
    )()

    messages = _messages(
        pre_image_prompt="COMMON_PREFIX",
        post_image_prompt="VARIABLE_TASK",
        context=context,  # type: ignore[arg-type]
    )
    content = messages[1]["content"]

    assert content[0] == {"type": "text", "text": "COMMON_PREFIX"}
    assert content[1]["type"] == "image_url"
    assert content[2] == {"type": "text", "text": "VARIABLE_TASK"}


def test_relation_message_is_text_only_and_has_no_contract_image_dependency() -> None:
    context = type(
        "Context",
        (),
        {"images": [{"data_url": "data:image/png;base64,AA=="}]},
    )()

    messages = _messages(
        pre_image_prompt="CURRENT_FIELD",
        post_image_prompt="TARGET_FIELD",
        context=context,  # type: ignore[arg-type]
        include_images=False,
        system_message=FIELD_RELATION_SYSTEM_MESSAGE,
    )

    assert messages[0]["content"] == FIELD_RELATION_SYSTEM_MESSAGE
    assert messages[1]["content"] == [
        {"type": "text", "text": "CURRENT_FIELD"},
        {"type": "text", "text": "TARGET_FIELD"},
    ]


def test_reason_fields_precede_model_decisions_and_allow_longer_explanations() -> None:
    candidate_properties = CandidateProposal.model_json_schema()["properties"]
    relation_properties = RelationComparison.model_json_schema()["properties"]
    single_relation_properties = SingleRelationJudgement.model_json_schema()["properties"]

    assert list(candidate_properties)[-2:] == ["novelty_reason", "status"]
    assert candidate_properties["novelty_reason"]["maxLength"] == 1200
    assert list(relation_properties)[-2:] == ["reason", "relation"]
    assert relation_properties["reason"]["maxLength"] == 1200
    assert list(single_relation_properties) == ["reason", "relation"]
    assert single_relation_properties["reason"]["maxLength"] == 1200


def test_prompts_explicitly_require_reason_then_decision() -> None:
    discovery_prompt = build_discovery_prompt_before_images(
        core_definitions=(),
        attribute_definitions=(),
        max_candidates=5,
    )
    target = record(field_id="transport_mode", name="运输方式")
    relation_prompt = build_relation_prompt(
        proposal=record(field_id="delivery_mode", name="交付方式"),
        match=CandidateMatch("candidate_0001", "group_0001", 0.01, 1),
        pool=SimpleNamespace(
            identity=lambda candidate_id: SimpleNamespace(
                candidate_id=candidate_id,
                group_id="group_0001",
                proposal=target,
            )
        ),
    )

    assert "最后两个键必须依次为 novelty_reason、status" in discovery_prompt
    assert "因此 status=accepted" in discovery_prompt
    assert "每个 Top 候选都会单独判别" in relation_prompt.preamble
    assert "最后两个键必须依次为 reason、relation" in relation_prompt.target
    assert "因此 relation=<本对象的 relation 值>" in relation_prompt.target
    assert "因此 relation=unrelated" in relation_prompt.target


def test_discovery_prompt_places_static_constraints_before_images_and_statuses_after() -> None:
    before_images = build_discovery_prompt_before_images(
        core_definitions=(), attribute_definitions=(), max_candidates=5
    )
    after_images = build_discovery_prompt_after_images(
        core_status_context="固定 Core 状态",
        attribute_status_context="固定 Attribute 状态",
        page_visibility_context="物理页码说明",
    )

    assert "固定 Discovery Core（覆盖约束）" in before_images
    assert "固定 Core 状态" not in before_images
    assert "固定 Core 状态" in after_images
    assert "物理页码说明" in after_images
    assert "evidence 保存当前合同的页码和原文位置" in before_images
    assert "extraction_rule 是字段库级跨合同规则" in before_images


def test_ide_configuration_uses_the_same_parser_as_cli_arguments() -> None:
    args = parse_args(
        build_ide_argv(
            input_dir="custom/input",
            output_dir="custom/output",
            core_catalog="custom/discovery_core.yaml",
            attribute_catalog=None,
            max_candidates_per_document=3,
            top_k=4,
            max_candidate_rule_retries=1,
            max_members_per_group=12,
            max_group_validation_retries=1,
        )
    )

    assert args.input_dir == Path("custom/input")
    assert args.output_dir == Path("custom/output")
    assert args.core_catalog == Path("custom/discovery_core.yaml")
    assert args.attribute_catalog is None
    assert args.max_candidates_per_document == 3
    assert args.top_k == 4
    assert args.max_candidate_rule_retries == 1
    assert args.max_members_per_group == 12
    assert args.max_group_validation_retries == 1
