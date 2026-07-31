"""Clause 结构发现、边界整理、复核、抽取与前缀复用回归测试。"""

import importlib.util
import sys
from pathlib import Path

import fitz
import pytest
from pydantic import ValidationError
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLAUSE_RUN_PATH = PROJECT_ROOT / "experiments/clause_extraction/run.py"
CORE_RUN_PATH = PROJECT_ROOT / "experiments/core_field_extraction/run.py"
PROMPT_ROOT = PROJECT_ROOT / "experiments/clause_extraction/prompts"
SHARED_PREFIX_PATH = PROJECT_ROOT / "experiments/prompts/00_contract_pdf_common_prefix.txt"
CLAUSE_SPEC_PATH = PROJECT_ROOT / "description/fields/clause/clause.yaml"


def load_experiment_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_clause_experiment():
    return load_experiment_module(CLAUSE_RUN_PATH, "clause_extraction_experiment")


def candidate(**overrides) -> dict:
    payload = {
        "parent_clause_number": "四",
        "parent_heading": "甲方责任与义务",
        "item_marker": "（1）",
        "item_heading": None,
        "opening_anchor": "（1）甲方应按约提供资料",
        "page_refs": [1],
    }
    payload.update(overrides)
    return payload


def consolidated(experiment, *, indices=None, end_before_anchor=None, **overrides):
    return experiment.ConsolidatedClauseCandidate.model_validate(
        {
            **candidate(**overrides),
            "source_candidate_indices": indices or [1],
            "end_before_anchor": end_before_anchor,
        }
    )


def test_step1_schema_is_structural_and_allows_empty_map() -> None:
    experiment = load_clause_experiment()
    schema = experiment.ClauseStructureMap.model_json_schema()

    assert list(schema["properties"]) == ["candidates"]
    assert experiment.ClauseStructureMap(candidates=[]).candidates == []
    item_schema = experiment.ClauseStructureCandidate.model_json_schema()["properties"]
    assert list(item_schema) == [
        "parent_clause_number",
        "parent_heading",
        "item_marker",
        "item_heading",
        "opening_anchor",
        "page_refs",
    ]


def test_core_and_all_clause_stages_use_identical_multimodal_prefix() -> None:
    clause = load_clause_experiment()
    core = load_experiment_module(CORE_RUN_PATH, "core_prefix_experiment")
    common_prefix = SHARED_PREFIX_PATH.read_text(encoding="utf-8").strip()
    images = [{"data_url": "data:image/png;base64,AA=="}]

    core_messages = core.messages_for(common_prefix, images, "Core 任务")
    for suffix in (
        "Clause Step 1",
        "Clause Step 1B",
        "Clause Step 2",
        "Clause Step 3",
    ):
        clause_messages = clause.messages_for(common_prefix, images, suffix)
        assert clause.SYSTEM_MESSAGE == core.SYSTEM_MESSAGE
        assert clause_messages[0] == core_messages[0]
        assert clause_messages[1]["content"][:-1] == core_messages[1]["content"][:-1]


def test_final_clause_schema_still_has_only_five_fields() -> None:
    experiment = load_clause_experiment()
    schema = experiment.ClauseItem.model_json_schema()

    assert list(schema["properties"]) == [
        "clause_number",
        "heading",
        "category",
        "source_text",
        "page_refs",
    ]
    with pytest.raises(ValidationError):
        experiment.ClauseItem(
            clause_number="3.10",
            heading=None,
            category="payment",
            source_text="甲方应按约付款。",
            page_refs=[2],
        )


def test_structure_map_validation_checks_page_order() -> None:
    experiment = load_clause_experiment()
    structure_map = experiment.ClauseStructureMap.model_validate(
        {"candidates": [candidate(page_refs=[2, 1])]}
    )

    errors = experiment.validate_structure_map(structure_map, 2)

    assert any("page_refs 必须按升序" in error for error in errors)


def test_boundary_plan_requires_complete_non_overlapping_accounting() -> None:
    experiment = load_clause_experiment()
    structure_map = experiment.ClauseStructureMap.model_validate(
        {
            "candidates": [
                candidate(item_heading="付款方式", item_marker="2.1"),
                candidate(opening_anchor="甲方应于签约后付款。"),
                candidate(item_heading="交付方式", item_marker="2.2"),
            ]
        }
    )
    valid = experiment.ClauseBoundaryPlan.model_validate(
        {
            "groups": [
                {
                    "source_candidate_indices": [1, 2],
                    "heading_strategy": "own",
                    "heading_candidate_index": 1,
                }
            ],
            "ignored_candidate_indices": [3],
        }
    )
    invalid = experiment.ClauseBoundaryPlan.model_validate(
        {
            "groups": [
                {
                    "source_candidate_indices": [1, 2],
                    "heading_strategy": "own",
                    "heading_candidate_index": 1,
                },
                {
                    "source_candidate_indices": [2, 3],
                    "heading_strategy": "own",
                    "heading_candidate_index": 2,
                },
            ],
            "ignored_candidate_indices": [],
        }
    )

    assert experiment.validate_boundary_plan(valid, structure_map) == []
    errors = experiment.validate_boundary_plan(invalid, structure_map)
    assert any("不得重叠" in error for error in errors)
    assert any("恰好核销" in error for error in errors)


def test_boundary_group_materialization_uses_next_atom_as_hard_end() -> None:
    experiment = load_clause_experiment()
    structure_map = experiment.ClauseStructureMap.model_validate(
        {
            "candidates": [
                candidate(
                    parent_clause_number="2.",
                    parent_heading="付款",
                    item_marker="2.1",
                    item_heading="付款方式",
                    opening_anchor="2.1 付款方式",
                    page_refs=[2],
                ),
                candidate(opening_anchor="甲方应在十日内付款。", page_refs=[2, 3]),
                candidate(
                    parent_clause_number="2.",
                    parent_heading="交付",
                    item_marker="2.2",
                    item_heading="交付方式",
                    opening_anchor="2.2 交付方式",
                    page_refs=[3],
                ),
            ]
        }
    )
    plan = experiment.ClauseBoundaryPlan.model_validate(
        {
            "groups": [
                {
                    "source_candidate_indices": [1, 2],
                    "heading_strategy": "own",
                    "heading_candidate_index": 1,
                },
                {
                    "source_candidate_indices": [3],
                    "heading_strategy": "own",
                    "heading_candidate_index": 3,
                },
            ],
            "ignored_candidate_indices": [],
        }
    )

    first, second = experiment.build_consolidated_candidates(plan, structure_map)

    assert first.source_candidate_indices == [1, 2]
    assert first.page_refs == [2, 3]
    assert first.end_before_anchor == "2.2 交付方式"
    assert second.end_before_anchor is None


def test_review_projection_fuses_parent_and_item_heading() -> None:
    experiment = load_clause_experiment()
    candidates = [
        consolidated(
            experiment,
            indices=[1, 2],
            end_before_anchor="◆ 本合同自签订之日起生效。",
            parent_heading="附加信息",
            item_marker="◆",
            item_heading="付款方式",
            opening_anchor="◆ 付款方式：全款到发货。",
        ),
        consolidated(
            experiment,
            indices=[3],
            parent_heading="附加信息",
            item_marker="◆",
            item_heading=None,
            opening_anchor="◆ 本合同自签订之日起生效。",
        ),
        consolidated(
            experiment, indices=[4], parent_heading=None, item_heading=None
        ),
    ]
    projections = experiment.build_review_projections(candidates)

    assert [item.fused_heading for item in projections] == [
        "附加信息 / 付款方式",
        "附加信息",
        None,
    ]
    assert "条款组序号 1" in projections[0].location
    assert "Step 1 原子 1、2" in projections[0].location
    assert "物理页 1" in projections[0].location
    assert "原文标记：◆" in projections[0].location
    assert "在此锚点前结束" in projections[0].location


def test_duplicate_parent_and_item_heading_is_not_repeated() -> None:
    experiment = load_clause_experiment()
    raw = experiment.ClauseStructureCandidate.model_validate(
        candidate(parent_heading="付款方式", item_heading="付款方式")
    )

    assert experiment.fuse_candidate_headings(raw) == "付款方式"


def test_null_fused_heading_cannot_be_included() -> None:
    experiment = load_clause_experiment()
    projection = experiment.ClauseReviewProjection(
        candidate_index=1,
        fused_heading=None,
        location="候选序号 1；物理页 1；原文开头：供方",
    )
    include = experiment.ClauseRetentionDecision(reason="误判为条款", decision="include")
    exclude = experiment.ClauseRetentionDecision(reason="没有标题", decision="exclude")

    assert experiment.validate_retention_decision(projection, include)
    assert experiment.validate_retention_decision(projection, exclude) == []


def test_decisions_only_select_consolidated_candidates() -> None:
    experiment = load_clause_experiment()
    candidates = [
        consolidated(experiment, indices=[1, 2]),
        consolidated(experiment, indices=[3], item_marker="（2）"),
    ]
    decisions = [
        experiment.ClauseRetentionDecision(reason="表达义务", decision="include"),
        experiment.ClauseRetentionDecision(reason="事实资料", decision="exclude"),
    ]

    selected = experiment.select_reviewed_candidates(candidates, decisions)

    assert len(selected) == 1
    assert selected[0] is candidates[0]
    assert selected[0].source_candidate_indices == [1, 2]


def test_explicit_bullet_and_label_are_deterministically_normalized() -> None:
    experiment = load_clause_experiment()
    raw = experiment.ClauseStructureCandidate.model_validate(
        candidate(
            parent_clause_number=None,
            parent_heading="附加信息",
            item_marker=None,
            item_heading=None,
            opening_anchor="◆ 付款方式：全款到发货。",
        )
    )

    normalized = experiment.normalize_candidate_structure(raw)

    assert normalized.item_marker == "◆"
    assert normalized.item_heading == "付款方式"


def test_top_level_number_is_not_reparsed_as_child_marker() -> None:
    experiment = load_clause_experiment()
    raw = experiment.ClauseStructureCandidate.model_validate(
        candidate(
            parent_clause_number="七",
            parent_heading="争议解决方式",
            item_marker=None,
            item_heading=None,
            opening_anchor="七、争议解决方式：双方应协商解决争议。",
        )
    )

    normalized = experiment.normalize_candidate_structure(raw)
    grouped = experiment.ConsolidatedClauseCandidate(
        **normalized.model_dump(), source_candidate_indices=[1]
    )
    unit = experiment.resolve_reviewed_units([grouped])[0]

    assert normalized.item_marker is None
    assert normalized.item_heading is None
    assert unit.clause_number == "七"


def test_parent_prefix_does_not_rewrite_existing_child_marker_or_heading_source() -> None:
    experiment = load_clause_experiment()
    raw = experiment.ClauseStructureCandidate.model_validate(
        candidate(
            parent_clause_number="四",
            parent_heading="甲方责任与义务",
            item_marker="（1）",
            item_heading=None,
            opening_anchor="四、甲方责任与义务：（1）甲方必须提供完整图纸。",
        )
    )

    normalized = experiment.normalize_candidate_structure(raw)
    grouped = experiment.ConsolidatedClauseCandidate(
        **normalized.model_dump(), source_candidate_indices=[1]
    )
    unit = experiment.resolve_reviewed_units([grouped])[0]

    assert normalized.item_marker == "（1）"
    assert normalized.item_heading is None
    assert unit.clause_number == "四（1）"
    assert unit.heading_source == "inherited"


def test_clause_number_combination_defensively_deduplicates_parent_marker() -> None:
    experiment = load_clause_experiment()

    assert experiment.combine_clause_number("七", "七、") == "七"
    assert experiment.combine_clause_number("7.", "7．") == "7."
    assert experiment.combine_clause_number("四", "（1）") == "四（1）"
    assert experiment.combine_clause_number("2.", "2.1") == "2.1"
    assert experiment.combine_clause_number("7.", "1)") == "7.1"


def test_separate_parent_heading_is_propagated_to_bulleted_children() -> None:
    experiment = load_clause_experiment()
    structure_map = experiment.ClauseStructureMap.model_validate(
        {
            "candidates": [
                candidate(
                    parent_clause_number=None,
                    parent_heading=None,
                    item_marker=None,
                    item_heading=None,
                    opening_anchor="附加信息 Additional Information",
                ),
                candidate(
                    parent_clause_number=None,
                    parent_heading=None,
                    item_marker=None,
                    item_heading=None,
                    opening_anchor="◆ 付款方式：全款到发货。",
                ),
                candidate(
                    parent_clause_number=None,
                    parent_heading=None,
                    item_marker=None,
                    item_heading=None,
                    opening_anchor="◆ 本合同自签订之日起生效。",
                ),
            ]
        }
    )

    normalized, _ = experiment.normalize_structure_map(structure_map)
    plan = experiment.ClauseBoundaryPlan.model_validate(
        {
            "groups": [
                {
                    "source_candidate_indices": [1],
                    "heading_strategy": "own",
                    "heading_candidate_index": 1,
                },
                {
                    "source_candidate_indices": [2],
                    "heading_strategy": "own",
                    "heading_candidate_index": 2,
                },
                {
                    "source_candidate_indices": [3],
                    "heading_strategy": "inherit_parent",
                    "heading_candidate_index": 3,
                },
            ],
            "ignored_candidate_indices": [],
        }
    )
    grouped = experiment.build_consolidated_candidates(plan, normalized)
    projections = experiment.build_review_projections(grouped)

    assert [item.fused_heading for item in projections] == [
        "附加信息 Additional Information",
        "附加信息 Additional Information / 付款方式",
        "附加信息 Additional Information",
    ]


def test_colon_heading_is_propagated_to_adjacent_body() -> None:
    experiment = load_clause_experiment()
    structure_map = experiment.ClauseStructureMap.model_validate(
        {
            "candidates": [
                candidate(
                    parent_heading=None,
                    item_marker=None,
                    item_heading=None,
                    opening_anchor="质量标准及责任限制：",
                ),
                candidate(
                    parent_heading=None,
                    item_marker=None,
                    item_heading=None,
                    opening_anchor="供方按照约定的规格型号提供产品。",
                ),
            ]
        }
    )

    normalized, _ = experiment.normalize_structure_map(structure_map)

    assert [item.parent_heading for item in normalized.candidates] == [
        "质量标准及责任限制",
        "质量标准及责任限制",
    ]


def test_et_style_numbered_label_uses_own_heading() -> None:
    experiment = load_clause_experiment()
    raw = experiment.ClauseStructureCandidate.model_validate(
        candidate(
            parent_clause_number=None,
            parent_heading=None,
            item_marker="(3)",
            item_heading="交货日期",
            opening_anchor="(3)交货日期：合同生效后",
        )
    )

    grouped = experiment.ConsolidatedClauseCandidate(
        **raw.model_dump(), source_candidate_indices=[1]
    )
    unit = experiment.resolve_reviewed_units([grouped])[0]

    assert unit.clause_number == "(3)"
    assert unit.heading == "交货日期"
    assert unit.heading_source == "own"


def test_beijing_style_untitled_child_inherits_parent_heading() -> None:
    experiment = load_clause_experiment()
    raw = consolidated(experiment)

    unit = experiment.resolve_reviewed_units([raw])[0]

    assert unit.clause_number == "四（1）"
    assert unit.heading == "甲方责任与义务"
    assert unit.heading_source == "inherited"


def test_shenzhen_style_child_prefers_own_heading_and_bullet_is_not_number() -> None:
    experiment = load_clause_experiment()
    own_raw = consolidated(
        experiment,
        parent_clause_number=None,
        parent_heading="附加信息",
        item_marker="◆",
        item_heading="付款方式",
    )
    inherited_raw = consolidated(
        experiment,
        indices=[2],
        parent_clause_number=None,
        parent_heading="附加信息",
        item_marker="◆",
        item_heading=None,
    )

    own, inherited = experiment.resolve_reviewed_units([own_raw, inherited_raw])

    assert (own.clause_number, own.heading, own.heading_source) == (
        None,
        "付款方式",
        "own",
    )
    assert (inherited.clause_number, inherited.heading, inherited.heading_source) == (
        None,
        "附加信息",
        "inherited",
    )


def test_unit_without_own_or_parent_heading_is_rejected() -> None:
    experiment = load_clause_experiment()
    units = [
        consolidated(experiment, parent_heading=None, item_heading=None),
        consolidated(
            experiment,
            indices=[2],
            parent_heading=None,
            item_heading=None,
            opening_anchor="另一个事实项",
        ),
    ]

    errors = experiment.collect_resolution_errors(units)

    assert len(errors) == 2
    with pytest.raises(ValueError, match=r"extraction_units\[2\]"):
        experiment.resolve_reviewed_units(units)


def test_heading_validation_is_conditional_on_heading_source() -> None:
    experiment = load_clause_experiment()
    inherited_unit = experiment.ResolvedClauseUnit(
        source_candidate_indices=[1],
        clause_number="四（1）",
        heading="甲方责任与义务",
        heading_source="inherited",
        opening_anchor="（1）甲方应按约提供资料",
        end_before_anchor="（2）乙方应按约验收",
        page_refs=[1],
    )
    extraction = experiment.ClauseUnitExtraction.model_validate(
        {
            "clauses": [
                {
                    "clause_number": "四（1）",
                    "heading": "甲方责任与义务",
                    "category": "rights_and_obligations",
                    "source_text": "（1）甲方应按约提供资料。",
                    "page_refs": [1],
                }
            ]
        }
    )

    assert experiment.validate_clause_unit_extraction(
        extraction, inherited_unit, 1, unit_index=1
    ) == []
    own_unit = inherited_unit.model_copy(update={"heading_source": "own"})
    errors = experiment.validate_clause_unit_extraction(extraction, own_unit, 1, unit_index=1)
    assert any("标题字符之间允许空白" in error for error in errors)


def test_own_heading_validation_ignores_unicode_whitespace() -> None:
    experiment = load_clause_experiment()
    unit = experiment.ResolvedClauseUnit(
        source_candidate_indices=[8],
        clause_number="(7)",
        heading="运输",
        heading_source="own",
        opening_anchor="(7) 运 输：卖方承担运输费用。",
        page_refs=[1],
    )
    extraction = experiment.ClauseUnitExtraction.model_validate(
        {
            "clauses": [
                {
                    "clause_number": "(7)",
                    "heading": "运输",
                    "category": "delivery_or_service",
                    "source_text": "(7) 运\n\t输：卖方承担运输费用。",
                    "page_refs": [1],
                }
            ]
        }
    )

    assert experiment.validate_clause_unit_extraction(
        extraction, unit, 1, unit_index=5
    ) == []


def test_unit_validation_rejects_wrong_start_and_swallowed_next_clause() -> None:
    experiment = load_clause_experiment()
    unit = experiment.ResolvedClauseUnit(
        source_candidate_indices=[1, 2],
        clause_number="2.1",
        heading="付款方式",
        heading_source="own",
        opening_anchor="2.1 付款方式",
        end_before_anchor="2.2 交付方式",
        page_refs=[1],
    )
    extraction = experiment.ClauseUnitExtraction.model_validate(
        {
            "clauses": [
                {
                    "clause_number": "2.1",
                    "heading": "付款方式",
                    "category": "payment",
                    "source_text": "付款方式：十日内付款。2.2 交付方式：送货上门。",
                    "page_refs": [1],
                }
            ]
        }
    )

    errors = experiment.validate_clause_unit_extraction(
        extraction, unit, 1, unit_index=1
    )

    assert any("opening_anchor" in error for error in errors)
    assert any("end_before_anchor" in error for error in errors)


def test_entire_pdf_renderer_rejects_truncation(tmp_path: Path) -> None:
    experiment = load_clause_experiment()
    pdf_path = tmp_path / "two-pages.pdf"
    document = fitz.open()
    document.new_page()
    document.new_page()
    document.save(pdf_path)
    document.close()

    with pytest.raises(ValueError, match="Clause 实验不会截断合同"):
        experiment.render_entire_pdf_as_data_urls(pdf_path, max_pages=1)


def test_clause_catalog_and_four_prompts_are_in_sync() -> None:
    experiment = load_clause_experiment()
    spec = yaml.safe_load(CLAUSE_SPEC_PATH.read_text(encoding="utf-8"))
    step1 = (PROMPT_ROOT / "01_discover_clause_structure.txt").read_text(encoding="utf-8")
    step1b = (PROMPT_ROOT / "01b_consolidate_clause_boundaries.txt").read_text(
        encoding="utf-8"
    )
    step2 = (PROMPT_ROOT / "02_review_clause_candidates.txt").read_text(encoding="utf-8")
    step3 = (PROMPT_ROOT / "03_extract_clause_unit.txt").read_text(encoding="utf-8")

    assert spec["schema_version"] == "0.4"
    assert list(experiment.ClauseItem.model_json_schema()["properties"]) == list(
        spec["output"]["items"]["properties"]
    )
    assert "不判断某项最终是否属于合同条款" in step1
    assert "(n) 标签：正文" in step1
    assert "互不重叠" in step1b
    assert "{{STRUCTURE_MAP}}" in step1b
    assert "标题原子 + 紧随的正文原子" in step1b
    assert "fused_heading" in step2
    assert "不得重写、补充、拆分、合并或复制" in step2
    assert "{{REVIEW_CANDIDATES}}" in step2
    assert "{{CURRENT_CANDIDATE}}" in step2
    assert "逐候选" in step2
    assert "质保" in step1 and "售后服务" in step2
    assert "heading_source=inherited" in step3
    assert "end_before_anchor" in step3
    assert "{{CATEGORY_VALUES}}" in step3


def test_exact_duplicates_are_reported_without_deletion() -> None:
    experiment = load_clause_experiment()
    item = experiment.ClauseItem(
        clause_number="3.1",
        heading="付款方式",
        category="payment",
        source_text="付款方式：甲方应按约付款。",
        page_refs=[2],
    )

    assert experiment.find_exact_duplicate_clauses([item, item]) == [
        {"first_index": 1, "duplicate_index": 2}
    ]


def test_source_overlap_and_duplicate_numbers_are_hard_validation_signals() -> None:
    experiment = load_clause_experiment()
    parent = experiment.ClauseItem(
        clause_number="3.1",
        heading="责任",
        category="rights_and_obligations",
        source_text="责任：甲方应提供完整技术资料，乙方应按照约定完成验收工作。",
        page_refs=[2],
    )
    child = experiment.ClauseItem(
        clause_number="3.1",
        heading="验收",
        category="acceptance",
        source_text="乙 方 应 按 照 约 定 完 成 验 收 工 作。",
        page_refs=[2],
    )
    duplicate_source = child.model_copy(
        update={"clause_number": "3.2", "heading": "验收责任"}
    )

    assert experiment.find_duplicate_source_texts([child, duplicate_source]) == [
        {"first_index": 1, "duplicate_index": 2}
    ]
    assert experiment.find_source_text_containments([parent, child]) == [
        {"container_index": 1, "contained_index": 2}
    ]
    assert experiment.find_duplicate_clause_numbers([parent, child]) == [
        {"clause_number": "3.1", "indices": [1, 2]}
    ]
