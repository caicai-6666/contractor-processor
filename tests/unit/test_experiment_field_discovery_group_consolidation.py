"""字段发现关系图分组与两阶段字段收敛的无模型回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.field_discovery_group_consolidation.merger import (
    DiscardedCandidate,
    FinalFieldDefinitionSuggestion,
    GlobalSemanticDecision,
    GlobalSemanticGateResponse,
    GroupOwnershipPlan,
    OwnershipFieldPlan,
    SingleGlobalSemanticDecision,
    build_final_field_definition_prompt,
    build_group_ownership_prompt,
    finalize_discard_reason,
    finalize_group_reason,
    finalize_singleton_group,
    load_group_profiles,
    validate_batch_field_ids,
    validate_final_field_definition,
    validate_global_semantic_gate,
    validate_group_ownership_plan,
    validate_single_global_semantic_decision,
)
from experiments.field_discovery_group_consolidation.run import build_ide_argv, parse_args
from experiments.field_discovery_group_consolidation.service import (
    build_refinement_plan,
    refine_group,
)
from experiments.field_discovery_stage_one.field_description import OutputDescription


def _identity(candidate_id: str, group_id: str, name: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "group_id": group_id,
        "suggested_definition": {
            "field_id": candidate_id.replace("candidate_", "field_"),
            "name": name,
            "meaning": f"{name}在合同履行中的稳定业务语义。",
            "aliases": [],
            "not_meaning": [],
            "output": {"type": "string", "nullable": True},
            "extraction_rule": f"仅在合同明确约定{name}时提取，未明确则返回空值。",
        },
        "statistics": {"occurrence_count": 2, "contract_count": 2},
    }


def _plan(
    *candidate_ids: str,
    plan_id: str = "field_plan_01",
    name: str = "交货方式",
) -> OwnershipFieldPlan:
    return OwnershipFieldPlan(
        plan_id=plan_id,
        source_candidate_ids=list(candidate_ids),
        name=name,
        meaning=f"合同明确约定的{name}及其稳定业务边界。",
        boundary=f"只记录{name}事实，不包含同组其他可独立治理事项。",
    )


def _suggestion(
    *, field_id: str = "delivery_method", name: str = "交货方式"
) -> FinalFieldDefinitionSuggestion:
    return FinalFieldDefinitionSuggestion(
        field_id=field_id,
        name=name,
        meaning=f"合同明确约定的{name}及其规范化结果。",
        output=OutputDescription(type="string"),
        extraction_rule=f"仅提取合同明确约定且与{name}直接关联的事实；未明确则返回空值。",
    )


def test_ownership_prompt_separates_unique_disposition_from_output_generation() -> None:
    profile = load_group_profiles(
        [
            _identity("candidate_0001", "group_0001", "运输方式"),
            _identity("candidate_0002", "group_0001", "交货方式"),
        ]
    )[0]

    prompt = build_group_ownership_prompt(profile=profile, max_members_per_group=8)

    assert "只决定合并、分拆或淘汰，不生成 output" in prompt
    assert "每个输入 candidate_id 必须且只能出现一次" in prompt
    assert "只与宽泛 object 的某个子字段相同" in prompt
    assert "因此 decision=refine_group" in prompt


def test_ownership_plan_requires_exact_candidate_coverage_once() -> None:
    profile = load_group_profiles(
        [
            _identity("candidate_0001", "group_0001", "运输方式"),
            _identity("candidate_0002", "group_0001", "交货方式"),
        ]
    )[0]
    valid = GroupOwnershipPlan(
        reason="两个候选描述同一交付方式事实。",
        decision="refine_group",
        final_field_plans=[_plan("candidate_0001", "candidate_0002")],
        discarded_candidates=[],
    )

    report = validate_group_ownership_plan(response=valid, profile=profile)

    assert report["reason"].endswith("因此 decision=refine_group")
    assert report["final_field_plans"][0]["source_candidate_ids"] == [
        "candidate_0001",
        "candidate_0002",
    ]

    duplicate = valid.model_copy(
        update={
            "discarded_candidates": [
                DiscardedCandidate(
                    candidate_id="candidate_0002",
                    reason="错误地重复分配同一候选。",
                    disposition="discarded",
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="重复 candidate_id"):
        validate_group_ownership_plan(response=duplicate, profile=profile)


def test_ownership_plan_cannot_split_same_identity_into_raw_and_structured_fields() -> None:
    profile = load_group_profiles(
        [
            _identity("candidate_0001", "group_0001", "交付方式"),
            _identity("candidate_0002", "group_0001", "交付方式"),
        ]
    )[0]
    split = GroupOwnershipPlan(
        reason="错误地按表示形式拆成原文版和结构化版。",
        decision="refine_group",
        final_field_plans=[
            _plan("candidate_0001", plan_id="field_plan_01", name="交付方式原文"),
            _plan("candidate_0002", plan_id="field_plan_02", name="交付方式分类"),
        ],
        discarded_candidates=[],
    )

    with pytest.raises(ValueError, match="表示差异不得拆成"):
        validate_group_ownership_plan(response=split, profile=profile)


def test_definition_stage_compiles_type_and_deterministically_fills_governance_lists() -> None:
    profile = load_group_profiles(
        [
            _identity("candidate_0001", "group_0001", "运输方式"),
            _identity("candidate_0002", "group_0001", "交货责任方"),
        ]
    )[0]
    current = _plan("candidate_0001", name="交货方式")
    sibling = _plan(
        "candidate_0002", plan_id="field_plan_02", name="交货责任方"
    )
    prompt = build_final_field_definition_prompt(
        profile=profile, plan=current, sibling_plans=(current, sibling)
    )

    field = validate_final_field_definition(
        suggestion=_suggestion(),
        plan=current,
        sibling_plans=(current, sibling),
        profile=profile,
    )

    assert "output 只描述 type" in prompt
    assert field.definition_record["output"] == {"type": "string", "nullable": True}
    assert field.definition_record["aliases"] == ["运输方式"]
    assert field.definition_record["not_meaning"] == ["交货责任方"]
    assert field.definition_record["examples"] == []


def test_definition_stage_rejects_document_location_and_fake_pattern_format() -> None:
    profile = load_group_profiles(
        [_identity("candidate_0001", "group_0001", "发票类型")]
    )[0]
    plan = _plan("candidate_0001", name="发票类型")

    with pytest.raises(ValueError, match="泛化性不足"):
        validate_final_field_definition(
            suggestion=_suggestion(field_id="invoice_type", name="发票类型").model_copy(
                update={"extraction_rule": "从条款(7)中提取发票类型。"}
            ),
            plan=plan,
            sibling_plans=(plan,),
            profile=profile,
        )

    with pytest.raises(ValueError, match="伪装正则约束"):
        validate_final_field_definition(
            suggestion=_suggestion(field_id="invoice_type", name="发票类型").model_copy(
                update={"output": OutputDescription(type="string", format="pattern: ^A")}
            ),
            plan=plan,
            sibling_plans=(plan,),
            profile=profile,
        )


def test_singleton_group_is_deterministic_and_needs_no_model_client() -> None:
    profile = load_group_profiles(
        [_identity("candidate_0001", "group_0001", "交货方式")]
    )[0]

    direct = finalize_singleton_group(profile)
    service = asyncio.run(
        refine_group(
            profile=profile,
            max_members_per_group=20,
            max_validation_retries=1,
            client=None,  # type: ignore[arg-type]
            settings=None,  # type: ignore[arg-type]
            limiter=None,  # type: ignore[arg-type]
        )
    )

    assert direct["decision"] == "passthrough_singleton"
    assert service["status"] == "succeeded"
    assert service["stages"]["ownership"]["status"] == "deterministic_passthrough"


def test_invalid_legacy_singleton_reports_its_real_validation_stage() -> None:
    identity = _identity("candidate_0001", "group_0001", "发票类型")
    identity["suggested_definition"]["extraction_rule"] = "在合同条款中查找发票类型。"  # type: ignore[index]
    profile = load_group_profiles([identity])[0]

    report = asyncio.run(
        refine_group(
            profile=profile,
            max_members_per_group=20,
            max_validation_retries=1,
            client=None,  # type: ignore[arg-type]
            settings=None,  # type: ignore[arg-type]
            limiter=None,  # type: ignore[arg-type]
        )
    )

    assert report["status"] == "failed"
    assert report["failed_stage"] == "singleton_validation"
    assert report["stages"]["definitions"][0]["error_type"] == (
        "ExtractionRuleGeneralizationError"
    )


def test_multigroup_schema_parse_failure_is_recorded_and_retried_once() -> None:
    class FakeCompletions:
        def __init__(self, payloads: list[str]) -> None:
            self.payloads = payloads

        async def create(self, **_: object) -> SimpleNamespace:
            content = self.payloads.pop(0)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content), finish_reason="stop"
                    )
                ],
                usage=SimpleNamespace(
                    model_dump=lambda: {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                ),
            )

    profile = load_group_profiles(
        [
            _identity("candidate_0001", "group_0001", "运输方式"),
            _identity("candidate_0002", "group_0001", "交货方式"),
        ]
    )[0]
    ownership = GroupOwnershipPlan(
        reason="两个候选记录同一个交货方式事实。",
        decision="refine_group",
        final_field_plans=[_plan("candidate_0001", "candidate_0002")],
        discarded_candidates=[],
    ).model_dump_json()
    definition = _suggestion().model_dump_json()
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(["{}", ownership, definition]))
    )
    generation = SimpleNamespace(
        max_completion_tokens=8192,
        temperature=0.0,
        top_p=1.0,
        presence_penalty=0.0,
        top_k=-1,
        repetition_penalty=1.0,
        seed=1,
    )
    settings = SimpleNamespace(
        models=SimpleNamespace(mllm=SimpleNamespace(model="fake", generation=generation))
    )

    report = asyncio.run(
        refine_group(
            profile=profile,
            max_members_per_group=20,
            max_validation_retries=1,
            client=fake_client,  # type: ignore[arg-type]
            settings=settings,  # type: ignore[arg-type]
            limiter=ModelRequestLimiter(1),
        )
    )

    attempts = report["stages"]["ownership"]["attempts"]
    assert report["status"] == "succeeded"
    assert [item["status"] for item in attempts] == ["rejected", "accepted"]
    assert attempts[0]["error_type"] == "StructuredInvocationError"
    assert attempts[0]["metrics"]["finish_reason"] == "stop"


def test_global_semantic_gate_requires_every_field_and_valid_targets() -> None:
    final_fields = [
        {
            "group_id": "group_0001",
            "definition": _identity("candidate_0001", "group_0001", "交货方式")[
                "suggested_definition"
            ],
        },
        {
            "group_id": "group_0002",
            "definition": _identity("candidate_0002", "group_0002", "运输方式")[
                "suggested_definition"
            ],
        },
    ]
    response = GlobalSemanticGateResponse(
        decisions=[
            GlobalSemanticDecision(
                final_field_ref="group_0001:field_0001",
                target_ref="group_0002:field_0002",
                reason="两个最终字段记录同一运输或配送事实。",
                status="duplicate_final",
            ),
            GlobalSemanticDecision(
                final_field_ref="group_0002:field_0002",
                target_ref="group_0001:field_0001",
                reason="与另一字段完整语义重复，不能分别推广。",
                status="duplicate_final",
            ),
        ]
    )

    report = validate_global_semantic_gate(
        response=response, final_fields=final_fields, fixed_definitions=()
    )

    assert report["status"] == "failed"
    assert report["conflict_count"] == 2


def test_single_global_gate_normalizes_unique_bare_fixed_reference() -> None:
    fixed = load_group_profiles(
        [_identity("candidate_0001", "group_0001", "合同金额")]
    )[0].members[0]
    # 测试只需要领域定义，沿用候选目录解析后的 FieldDefinition。
    from contract_processor.domain.enums import FieldKind
    from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog

    fixed_definition = YamlFieldCatalog._to_definition(fixed.definition, FieldKind.ATTRIBUTE)
    decision = validate_single_global_semantic_decision(
        response=SingleGlobalSemanticDecision(
            target_ref="field_0001",
            reason="当前字段完整等同于固定字段所记录的同一规范值。",
            status="covered_by_fixed",
        ),
        current_field_ref="group_0002:new_field",
        final_field_refs={"group_0002:new_field"},
        fixed_definitions=(fixed_definition,),
    )

    assert decision.target_ref == "fixed:field_0001"


def test_empty_plan_and_batch_id_gate_are_explicit() -> None:
    assert build_refinement_plan(profiles=(), reports=[]) == {
        "mode": "relation_graph_two_stage_refinement",
        "source_group_count": 0,
        "source_identity_count": 0,
        "succeeded_group_count": 0,
        "failed_group_count": 0,
        "final_field_count": 0,
        "discarded_candidate_count": 0,
        "batch_field_id_gate": "passed",
        "batch_semantic_gate": "not_run",
        "semantic_gate": {
            "status": "not_run",
            "decision_count": 0,
            "conflict_count": 0,
            "decisions": [],
            "attempts": [],
        },
        "final_fields": [],
    }

    duplicate_field = {
        "definition": {"field_id": "delivery_method"},
        "source_candidate_ids": ["candidate_0001"],
    }
    with pytest.raises(ValueError, match="重复最终 field_id"):
        validate_batch_field_ids(
            [
                {
                    "group_id": "group_0001",
                    "status": "succeeded",
                    "final_fields": [duplicate_field],
                },
                {
                    "group_id": "group_0002",
                    "status": "succeeded",
                    "final_fields": [duplicate_field],
                },
            ]
        )


def test_reasons_are_normalised_to_fixed_conclusions() -> None:
    assert finalize_group_reason(
        reason="同义候选已合并为一个稳定字段。", decision="refine_group"
    ).endswith("因此 decision=refine_group")
    assert finalize_discard_reason(
        reason="该候选是一次性内部编号。", disposition="discarded"
    ).endswith("因此 disposition=discarded")


def test_ide_arguments_use_the_cli_parser() -> None:
    args = parse_args(
        build_ide_argv(
            source_run="experiments/outputs/field_discovery_stage_one/example",
            output_dir="experiments/outputs/field_discovery_group_consolidation",
            max_members_per_group=6,
            max_validation_retries=1,
        )
    )
    assert args.source_run == Path("experiments/outputs/field_discovery_stage_one/example")
    assert args.max_members_per_group == 6
