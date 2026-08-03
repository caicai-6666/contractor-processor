#!/usr/bin/env python3
"""字段发现第一大步统一流水线：固定字段、候选建池、关系判别与组级收敛。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.application.prompts.pdf_prefix import (  # noqa: E402
    build_common_rules,
    build_page_visibility_context,
)
from contract_processor.domain.enums import FieldKind, RuntimeMode  # noqa: E402
from contract_processor.domain.models import FieldDefinition  # noqa: E402
from contract_processor.infrastructure.embedding import (  # noqa: E402
    Qwen3VLEmbeddingClient,
    load_contract_embedding_policy,
)
from contract_processor.infrastructure.extraction.attribute import (  # noqa: E402
    AttributeExtractionService,
    EmptyAttributeExtractionService,
)
from contract_processor.infrastructure.extraction.context import (  # noqa: E402
    PdfExtractionContext,
)
from contract_processor.infrastructure.extraction.core import (  # noqa: E402
    CoreExtractionService,
    EmptyCoreExtractionService,
)
from contract_processor.infrastructure.extraction.validated_pipelines import (  # noqa: E402
    ValidatedExtractionPipelines,
)
from contract_processor.infrastructure.llm.request_limiter import (  # noqa: E402
    ModelRequestLimiter,
)
from contract_processor.infrastructure.persistence.yaml_field_catalog import (  # noqa: E402
    YamlFieldCatalog,
)
from contract_processor.settings import (  # noqa: E402
    ProjectSettings,
    load_project_settings,
)
from experiments.field_discovery_stage_one.discovery import (  # noqa: E402
    CandidateProposal,
    CandidateProposalRecord,
    CandidateSemanticGateBatch,
    CandidateSemanticGateDecision,
    CandidateVectorPool,
    ExtractionRuleGeneralizationError,
    ExtractionRuleRevision,
    FIELD_RELATION_SYSTEM_MESSAGE,
    RelationComparison,
    RelationJudgement,
    SingleRelationJudgement,
    build_discovery_prompt_after_images,
    build_discovery_prompt_before_images,
    build_candidate_proposal_repair_prompt,
    build_single_candidate_semantic_gate_prompt,
    build_extraction_rule_revision_prompt,
    build_relation_prompt,
    invoke_candidate_proposals,
    invoke_structured,
    render_attribute_status_context,
    render_core_status_context,
    resolve_candidate_identity,
    validate_candidate_semantic_gate,
    validate_candidate_proposal,
    validate_extraction_rule_revision,
    validate_relation_judgement,
    validate_single_relation_semantics,
)
from experiments.field_discovery_group_consolidation.merger import (  # noqa: E402
    load_group_profiles,
)
from experiments.field_discovery_group_consolidation.service import (  # noqa: E402
    build_refinement_plan,
    refine_candidate_groups,
    run_global_semantic_gate,
)


EXTRACTION_RULE_REVISION_SYSTEM_MESSAGE = (
    "你只修订合同元数据字段的通用 extraction_rule。不得改写字段身份、输出结构或合同证据。"
)
CANDIDATE_SEMANTIC_GATE_SYSTEM_MESSAGE = (
    "你是合同字段发现的语义准入门禁。你只检查固定覆盖、字段原子性和规则一致性，"
    "不得改写候选或重新提取合同。"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行字段发现第一大步统一流水线")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/input"),
        help="待发现字段的 PDF 目录，相对路径以项目根目录为准。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/outputs/field_discovery_stage_one"),
        help="实验产物根目录。",
    )
    parser.add_argument(
        "--core-catalog",
        type=Path,
        default=None,
        help="独立 Discovery Core YAML；默认读取 settings.paths.discovery_core_fields。",
    )
    parser.add_argument(
        "--attribute-catalog",
        type=Path,
        default=None,
        help="独立 Discovery Attribute YAML；默认读取 settings.paths.discovery_attribute_fields。",
    )
    parser.add_argument(
        "--max-candidates-per-document",
        type=int,
        default=5,
        choices=range(1, 6),
        metavar="1..5",
        help="每份合同最多提出的新字段数，默认 5。",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        choices=range(1, 6),
        metavar="1..5",
        help="只在新候选池中比较的最大候选数，默认 5。",
    )
    parser.add_argument(
        "--max-candidate-rule-retries",
        type=int,
        default=1,
        choices=range(0, 2),
        help="候选 extraction_rule 位置化时的局部修订次数，默认 1。",
    )
    parser.add_argument(
        "--max-members-per-group",
        type=int,
        default=20,
        help="最终组级收敛允许的单组最大字段数，默认 20。",
    )
    parser.add_argument(
        "--max-group-validation-retries",
        type=int,
        default=1,
        choices=range(0, 2),
        help="组级字段定义未通过程序门禁时的纠错次数，默认 1。",
    )
    return parser.parse_args(argv)


def build_ide_argv(
    *,
    input_dir: str,
    output_dir: str,
    core_catalog: str | None,
    attribute_catalog: str | None,
    max_candidates_per_document: int,
    top_k: int,
    max_candidate_rule_retries: int,
    max_members_per_group: int,
    max_group_validation_retries: int,
) -> list[str]:
    """把 IDE 编辑区的配置转换为与命令行完全相同的参数。"""

    argv = [
        "--input-dir",
        input_dir,
        "--output-dir",
        output_dir,
        "--max-candidates-per-document",
        str(max_candidates_per_document),
        "--top-k",
        str(top_k),
        "--max-candidate-rule-retries",
        str(max_candidate_rule_retries),
        "--max-members-per-group",
        str(max_members_per_group),
        "--max-group-validation-retries",
        str(max_group_validation_retries),
    ]
    if core_catalog:
        argv.extend(["--core-catalog", core_catalog])
    if attribute_catalog:
        argv.extend(["--attribute-catalog", attribute_catalog])
    return argv


def _resolve_project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _write_json_sync(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _append_line_sync(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def _sha256_file_sync(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _sanitized_error_record(error: Exception) -> dict[str, Any]:
    """失败日志只保留类型、简短原因和可观测指标，不保存模型原始响应。"""

    record: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error": (str(error).strip() or type(error).__name__)[:1200],
    }
    metrics = getattr(error, "metrics", None)
    if isinstance(metrics, dict):
        record["metrics"] = metrics
    return record


async def _snapshot_vllm_cache_metrics(base_url: str) -> dict[str, Any]:
    """尽力采集 vLLM 前缀/多模态缓存计数；不可用时不阻断字段发现实验。"""

    metrics_url = base_url.rstrip("/")
    if metrics_url.endswith("/v1"):
        metrics_url = metrics_url[:-3]
    metrics_url += "/metrics"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(metrics_url)
            response.raise_for_status()
    except Exception as error:
        return {
            "available": False,
            "error_type": type(error).__name__,
            "error": (str(error).strip() or type(error).__name__)[:300],
        }
    samples: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line or line.startswith("#"):
            continue
        metric, separator, raw_value = line.rpartition(" ")
        lowered = metric.casefold()
        if not separator or "cache" not in lowered or not any(
            marker in lowered for marker in ("hit", "quer", "request")
        ):
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        samples.append({"metric": metric, "value": value})
    return {"available": True, "metrics_url": metrics_url, "samples": samples[:200]}


class StageLogger:
    """控制台与 stage.log 同步输出，不记录合同字段值或模型原始内容。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def emit(self, message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}"
        await run_blocking(print, line, flush=True)
        await run_blocking(_append_line_sync, self._path, line)


async def _validate_candidate_with_rule_retry(
    *,
    proposal: CandidateProposal,
    fixed_definitions: Sequence[FieldDefinition],
    source_page_count: int,
    max_rule_retries: int,
    context: PdfExtractionContext,
    settings: ProjectSettings,
    schema_name: str,
) -> tuple[
    CandidateProposalRecord | None,
    CandidateProposal | None,
    list[dict[str, Any]],
    str | None,
]:
    """只为位置化 extraction_rule 做一次局部重试，其他门禁失败保持直接拒绝。

    局部响应只允许重写字段及子字段规则；程序会剥离规则文本后逐项比对 output，拒绝其他变化。
    """

    current = proposal
    attempts: list[dict[str, Any]] = []
    for attempt in range(max_rule_retries + 1):
        try:
            candidate = validate_candidate_proposal(
                current,
                fixed_definitions=fixed_definitions,
                source_page_count=source_page_count,
            )
        except ExtractionRuleGeneralizationError as error:
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "status": "rejected_by_gate",
                    "error": str(error),
                }
            )
            if attempt >= max_rule_retries:
                return None, None, attempts, str(error)
            try:
                revised, metrics = await invoke_structured(
                    client=context.client,
                    context=context,
                    settings=settings,
                    pre_image_prompt=build_extraction_rule_revision_prompt(
                        proposal=current, validation_error=str(error)
                    ),
                    post_image_prompt="请输出修订后的通用 extraction_rule。",
                    schema_model=ExtractionRuleRevision,
                    schema_name=schema_name,
                    max_completion_tokens=2048,
                    include_images=False,
                    system_message=EXTRACTION_RULE_REVISION_SYSTEM_MESSAGE,
                )
            except Exception as retry_error:
                error_record = _sanitized_error_record(retry_error)
                attempts.append(
                    {
                        "attempt": attempt + 2,
                        "status": "retry_failed",
                        **error_record,
                    }
                )
                return None, None, attempts, str(retry_error)
            assert isinstance(revised, ExtractionRuleRevision)
            attempts[-1]["retry_metrics"] = metrics
            try:
                revised_output = validate_extraction_rule_revision(
                    original_output=current.output,
                    revised_output=revised.output,
                )
            except ValueError as revision_error:
                attempts.append(
                    {
                        "attempt": attempt + 2,
                        "status": "retry_failed",
                        "error_type": type(revision_error).__name__,
                        "error": str(revision_error),
                    }
                )
                return None, None, attempts, str(revision_error)
            current = current.model_copy(
                update={
                    "extraction_rule": revised.extraction_rule,
                    "output": revised_output,
                }
            )
            continue
        except ValueError as error:
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "status": "rejected_by_gate",
                    "error": str(error),
                }
            )
            return None, None, attempts, str(error)
        attempts.append({"attempt": attempt + 1, "status": "accepted"})
        return candidate, current, attempts, None
    raise AssertionError("候选规则重试循环未返回结果。")


async def _retry_invalid_candidate_proposal(
    *,
    proposal_payload: object,
    validation_error: str,
    context: PdfExtractionContext,
    settings: ProjectSettings,
    schema_name: str,
) -> tuple[CandidateProposal | None, dict[str, Any]]:
    """为单个未通过 Schema 的候选提供一次无 PDF 结构修复机会。"""

    try:
        revised, metrics = await invoke_structured(
            client=context.client,
            context=context,
            settings=settings,
            pre_image_prompt=build_candidate_proposal_repair_prompt(
                proposal_payload=proposal_payload, validation_error=validation_error
            ),
            post_image_prompt="请输出修复后的单个候选字段 JSON。",
            schema_model=CandidateProposal,
            schema_name=schema_name,
            max_completion_tokens=4096,
            include_images=False,
        )
    except Exception as error:
        return None, {
            "status": "retry_failed",
            **_sanitized_error_record(error),
        }
    assert isinstance(revised, CandidateProposal)
    return revised, {"status": "accepted", "metrics": metrics}


async def _run_candidate_semantic_gate(
    *,
    candidates: Sequence[tuple[int, CandidateProposalRecord]],
    fixed_definitions: Sequence[FieldDefinition],
    context: PdfExtractionContext,
    settings: ProjectSettings,
    schema_name: str,
    max_validation_retries: int = 1,
) -> tuple[dict[int, Any | None], dict[str, Any]]:
    """并发执行单候选语义准入；单项失败不阻断同合同其他候选。"""

    if not candidates:
        return {}, {"status": "deterministic_empty", "attempts": []}

    async def judge_one(
        proposal_index: int, candidate: CandidateProposalRecord
    ) -> tuple[int, Any | None, dict[str, Any]]:
        prompt = build_single_candidate_semantic_gate_prompt(
            candidate_index=proposal_index,
            candidate=candidate,
            fixed_definitions=fixed_definitions,
        )
        attempts: list[dict[str, Any]] = []
        correction: str | None = None
        for attempt in range(max_validation_retries + 1):
            current_prompt = prompt
            if correction:
                current_prompt += (
                    "\n\n【上次输出的程序校验失败】\n"
                    + correction
                    + "\n请修正后只输出当前候选的语义准入结论。"
                )
            try:
                response, metrics = await invoke_structured(
                    client=context.client,
                    context=context,
                    settings=settings,
                    pre_image_prompt=current_prompt,
                    post_image_prompt="请输出当前候选的语义准入 JSON。",
                    schema_model=CandidateSemanticGateDecision,
                    schema_name=f"{schema_name}_{proposal_index:02d}",
                    max_completion_tokens=2048,
                    include_images=False,
                    system_message=CANDIDATE_SEMANTIC_GATE_SYSTEM_MESSAGE,
                )
                assert isinstance(response, CandidateSemanticGateDecision)
                decision = validate_candidate_semantic_gate(
                    CandidateSemanticGateBatch(decisions=[response]),
                    expected_indices=[proposal_index],
                    fixed_definitions=fixed_definitions,
                )[proposal_index]
            except Exception as error:
                error_record = _sanitized_error_record(error)
                correction = str(error_record["error"])
                attempts.append(
                    {"attempt": attempt + 1, "status": "rejected", **error_record}
                )
                continue
            attempts.append(
                {"attempt": attempt + 1, "status": "accepted", "metrics": metrics}
            )
            return proposal_index, decision, {"status": "succeeded", "attempts": attempts}
        return proposal_index, None, {"status": "failed", "attempts": attempts}

    results = await asyncio.gather(
        *(judge_one(proposal_index, candidate) for proposal_index, candidate in candidates)
    )
    decisions = {proposal_index: decision for proposal_index, decision, _ in results}
    records = {str(proposal_index): record for proposal_index, _, record in results}
    return decisions, {
        "status": "succeeded" if all(decision is not None for decision in decisions.values()) else "partial_failure",
        "candidates": records,
    }


async def _revise_semantically_invalid_rule(
    *,
    proposal: CandidateProposal,
    validation_error: str,
    fixed_definitions: Sequence[FieldDefinition],
    source_page_count: int,
    context: PdfExtractionContext,
    settings: ProjectSettings,
    schema_name: str,
) -> tuple[CandidateProposalRecord, CandidateProposal, dict[str, Any]]:
    """语义门禁只允许局部改写规则，随后重新执行结构、位置和领域字段契约。"""

    revised, metrics = await invoke_structured(
        client=context.client,
        context=context,
        settings=settings,
        pre_image_prompt=build_extraction_rule_revision_prompt(
            proposal=proposal, validation_error=validation_error
        ),
        post_image_prompt="请输出与当前字段语义一致的通用 extraction_rule。",
        schema_model=ExtractionRuleRevision,
        schema_name=schema_name,
        max_completion_tokens=2048,
        include_images=False,
        system_message=EXTRACTION_RULE_REVISION_SYSTEM_MESSAGE,
    )
    assert isinstance(revised, ExtractionRuleRevision)
    revised_output = validate_extraction_rule_revision(
        original_output=proposal.output, revised_output=revised.output
    )
    revised_proposal = proposal.model_copy(
        update={"extraction_rule": revised.extraction_rule, "output": revised_output}
    )
    record = validate_candidate_proposal(
        revised_proposal,
        fixed_definitions=fixed_definitions,
        source_page_count=source_page_count,
    )
    return record, revised_proposal, metrics


async def _judge_relation_with_retry(
    *,
    proposal: CandidateProposalRecord,
    match: Any,
    pool: CandidateVectorPool,
    context: PdfExtractionContext,
    settings: ProjectSettings,
    schema_name: str,
    max_validation_retries: int = 1,
) -> tuple[RelationComparison, list[dict[str, Any]]]:
    """逐对关系判定；解析失败和危险的顶层/子字段错配共享一次反馈重试。"""

    prompt = build_relation_prompt(proposal=proposal, match=match, pool=pool)
    correction: str | None = None
    attempts: list[dict[str, Any]] = []
    for attempt in range(max_validation_retries + 1):
        target_prompt = prompt.target
        if correction:
            target_prompt += (
                "\n\n【上次判定的程序校验失败】\n"
                + correction
                + "\n请重新比较两个顶层字段的完整业务边界。"
            )
        try:
            judgement, metrics = await invoke_structured(
                client=context.client,
                context=context,
                settings=settings,
                pre_image_prompt=prompt.preamble,
                post_image_prompt=target_prompt,
                schema_model=SingleRelationJudgement,
                schema_name=schema_name,
                max_completion_tokens=4096,
                include_images=False,
                system_message=FIELD_RELATION_SYSTEM_MESSAGE,
            )
            assert isinstance(judgement, SingleRelationJudgement)
            normalized = validate_single_relation_semantics(
                proposal=proposal,
                target=pool.identity(match.candidate_id).proposal,
                judgement=judgement,
            )
        except Exception as error:
            error_record = _sanitized_error_record(error)
            correction = str(error_record["error"])
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "status": "rejected",
                    **error_record,
                }
            )
            continue
        attempts.append(
            {"attempt": attempt + 1, "status": "accepted", "metrics": metrics}
        )
        return (
            RelationComparison(
                target_candidate_id=match.candidate_id,
                reason=normalized.reason,
                relation=normalized.relation,
            ),
            attempts,
        )
    raise RuntimeError(
        f"候选关系 {proposal.definition.field_id} -> {match.candidate_id} 在一次纠错后仍无有效结果。"
    )


async def _build_pipelines(
    *,
    settings: ProjectSettings,
    core_catalog_path: Path,
    attribute_catalog_path: Path,
) -> tuple[ValidatedExtractionPipelines, tuple[Any, ...], tuple[Any, ...]]:
    """用隔离目录组装共享 Core/Attribute 提取器，不触碰 production YAML。"""

    catalog = YamlFieldCatalog(
        core_path=core_catalog_path,
        attribute_path=attribute_catalog_path,
    )
    core_snapshot, attribute_snapshot = await asyncio.gather(
        catalog.snapshot(FieldKind.CORE), catalog.snapshot(FieldKind.ATTRIBUTE)
    )
    core_service = (
        EmptyCoreExtractionService(core_snapshot.schema_version)
        if core_snapshot.is_empty
        else CoreExtractionService(
            core_catalog_path=core_catalog_path,
            attribute_catalog_path=attribute_catalog_path,
        )
    )
    pipelines = ValidatedExtractionPipelines(
        PROJECT_ROOT,
        settings,
        runtime_mode=RuntimeMode.DISCOVERY,
        core_catalog_snapshot=core_snapshot,
        attribute_catalog_snapshot=attribute_snapshot,
        field_catalog=catalog,
        core_service=core_service,
        attribute_service=AttributeExtractionService(
            core_catalog_path=core_catalog_path,
            attribute_catalog_path=attribute_catalog_path,
        ),
        empty_attribute_service=EmptyAttributeExtractionService(attribute_catalog_path),
    )
    return pipelines, core_snapshot.definitions, attribute_snapshot.definitions


async def _process_document(
    *,
    index: int,
    total: int,
    pdf_path: Path,
    settings: ProjectSettings,
    core_catalog_path: Path,
    attribute_catalog_path: Path,
    pool: CandidateVectorPool,
    max_candidates: int,
    top_k: int,
    max_candidate_rule_retries: int,
    logger: StageLogger,
) -> dict[str, Any]:
    """完成单合同固定字段、发现、召回、判别和候选池更新。"""

    prefix = f"[{index:02d}/{total:02d}] {pdf_path.name}"
    pipelines, core_definitions, attribute_definitions = await _build_pipelines(
        settings=settings,
        core_catalog_path=core_catalog_path,
        attribute_catalog_path=attribute_catalog_path,
    )
    try:
        await logger.emit(
            f"{prefix} [PREPARE] Core={len(core_definitions)}，"
            f"Attribute={len(attribute_definitions)}，候选池={pool.size}"
        )
        prepared = await pipelines.prepare(pdf_path)
        await logger.emit(
            f"{prefix} [PREPARE] 完成：页数={prepared['source_page_count']}，"
            f"document_id={str(prepared['document_id'])[:12]}…"
        )

        await logger.emit(f"{prefix} [1/5 FIXED CORE] 开始固定 Core 提取")
        core_payload = await pipelines.extract_core(pdf_path)
        core_fields = core_payload["fields"]
        core_statuses = render_core_status_context(core_fields, core_definitions)
        await logger.emit(
            f"{prefix} [1/5 FIXED CORE] 完成：字段数={len(core_fields)}，"
            f"目录模式={pipelines.core_catalog_mode}"
        )

        await logger.emit(f"{prefix} [1/5 FIXED ATTRIBUTE] 开始固定 Attribute 提取")
        attribute_fields = await pipelines.extract_attributes(core_fields)
        attribute_statuses = render_attribute_status_context(
            attribute_fields, attribute_definitions
        )
        await logger.emit(
            f"{prefix} [1/5 FIXED ATTRIBUTE] 完成：字段数={len(attribute_fields)}，"
            f"目录模式={pipelines.attribute_catalog_mode}"
        )

        context = pipelines.prepared_context
        common_rules = await build_common_rules()
        discovery_pre_image_prompt = common_rules + "\n\n" + build_discovery_prompt_before_images(
            core_definitions=core_definitions,
            attribute_definitions=attribute_definitions,
            max_candidates=max_candidates,
        )
        discovery_post_image_prompt = build_discovery_prompt_after_images(
            core_status_context=core_statuses,
            attribute_status_context=attribute_statuses,
            page_visibility_context=build_page_visibility_context(
                context.source_page_count
            ),
        )
        await logger.emit(
            f"{prefix} [2/5 DISCOVER] 请求模型生成最多 {max_candidates} 个新字段"
        )
        parsed_candidates, parse_failures, discovery_metrics = await invoke_candidate_proposals(
            client=context.client,
            context=context,
            settings=settings,
            pre_image_prompt=discovery_pre_image_prompt,
            post_image_prompt=discovery_post_image_prompt,
            schema_name=f"field_discovery_candidates_{index:02d}",
            max_completion_tokens=6144,
        )
        generated_candidate_count = len(parsed_candidates) + len(parse_failures)
        await logger.emit(
            f"{prefix} [2/5 DISCOVER] 完成：模型返回 {generated_candidate_count} 个候选，"
            f"耗时={discovery_metrics['elapsed_seconds']}s"
        )

        candidate_records: list[dict[str, Any]] = []
        # 单个候选不符合 Pydantic Schema 时，不让它拖累同批已经合法的候选。
        # 每个失败项只获得一次无 PDF 的定点修复机会；修复失败才单独拒绝。
        generated_candidates = list(parsed_candidates)
        for failure in parse_failures:
            repaired, repair_attempt = await _retry_invalid_candidate_proposal(
                proposal_payload=failure.payload,
                validation_error=failure.error,
                context=context,
                settings=settings,
                schema_name=(
                    f"field_discovery_candidate_repair_{index:02d}_{failure.proposal_index:02d}"
                ),
            )
            if repaired is not None:
                generated_candidates.append((failure.proposal_index, repaired))
                await logger.emit(
                    f"{prefix} [2/5 REPAIR] 候选 {failure.proposal_index}/"
                    f"{generated_candidate_count} 结构修复通过"
                )
                continue
            raw_payload = failure.payload if isinstance(failure.payload, dict) else {}
            candidate_records.append(
                {
                    "proposal_index": failure.proposal_index,
                    "field_id": str(raw_payload.get("field_id", "<unparsed>")),
                    "name": str(raw_payload.get("name", "<unparsed>")),
                    "status": "rejected_by_gate",
                    "rejection_category": "proposal_schema",
                    "reason": failure.error,
                    "gates": {"proposal_schema_repair": repair_attempt},
                }
            )
            await logger.emit(
                f"{prefix} [2/5 REPAIR] 候选 {failure.proposal_index}/"
                f"{generated_candidate_count} 结构修复失败：{repair_attempt['error']}"
            )

        fixed_definitions = (*core_definitions, *attribute_definitions)
        locally_valid: list[dict[str, Any]] = []
        # 先完成确定性的结构、位置和领域契约校验，再把最多五个合格候选一次性交给语义门禁。
        # 这样 fixed coverage / atomicity 判断共享同一上下文，也不会让已知坏结构进入向量池。
        for proposal_index, proposal in sorted(generated_candidates, key=lambda item: item[0]):
            short = f"{prefix} [2/5 GATE] 候选 {proposal_index}/{generated_candidate_count}"
            candidate, validated_proposal, gate_attempts, gate_error = (
                await _validate_candidate_with_rule_retry(
                    proposal=proposal,
                    fixed_definitions=fixed_definitions,
                    source_page_count=context.source_page_count,
                    max_rule_retries=max_candidate_rule_retries,
                    context=context,
                    settings=settings,
                    schema_name=(
                        f"field_discovery_rule_revision_{index:02d}_{proposal_index:02d}"
                    ),
                )
            )
            if candidate is None or validated_proposal is None:
                await logger.emit(f"{short} 结构/规则拒绝：{gate_error}")
                candidate_records.append(
                    {
                        "proposal_index": proposal_index,
                        "field_id": proposal.field_id,
                        "name": proposal.name,
                        "status": "rejected_by_gate",
                        "rejection_category": "structure_or_rule",
                        "reason": gate_error,
                        "gates": {"structure_and_rule": gate_attempts},
                    }
                )
                continue
            locally_valid.append(
                {
                    "proposal_index": proposal_index,
                    "proposal": validated_proposal,
                    "candidate": candidate,
                    "structure_attempts": gate_attempts,
                }
            )

        semantic_decisions, semantic_gate_metrics = await _run_candidate_semantic_gate(
            candidates=[
                (item["proposal_index"], item["candidate"]) for item in locally_valid
            ],
            fixed_definitions=fixed_definitions,
            context=context,
            settings=settings,
            schema_name=f"field_discovery_semantic_gate_{index:02d}",
        )
        accepted_candidates: list[dict[str, Any]] = []
        for item in locally_valid:
            proposal_index = item["proposal_index"]
            proposal = item["proposal"]
            candidate = item["candidate"]
            decision = semantic_decisions[proposal_index]
            if decision is None:
                semantic_attempts = semantic_gate_metrics["candidates"][str(proposal_index)]
                reason = semantic_attempts["attempts"][-1]["error"]
                await logger.emit(
                    f"{prefix} [2/5 SEMANTIC GATE] 候选 {proposal_index} 调用失败：{reason}"
                )
                candidate_records.append(
                    {
                        "proposal_index": proposal_index,
                        "field_id": candidate.definition.field_id,
                        "name": candidate.definition.name,
                        "status": "rejected_by_gate",
                        "rejection_category": "semantic_gate_error",
                        "reason": reason,
                        "gates": {
                            "structure_and_rule": item["structure_attempts"],
                            "semantic": semantic_attempts,
                        },
                    }
                )
                continue
            semantic_rule_retry: dict[str, Any] | None = None
            # invalid_rule 是唯一允许局部改写的语义状态；字段身份、结构和证据全部锁定。
            if decision.status == "invalid_rule" and max_candidate_rule_retries > 0:
                try:
                    candidate, proposal, retry_metrics = (
                        await _revise_semantically_invalid_rule(
                            proposal=proposal,
                            validation_error=decision.reason,
                            fixed_definitions=fixed_definitions,
                            source_page_count=context.source_page_count,
                            context=context,
                            settings=settings,
                            schema_name=(
                                f"field_discovery_semantic_rule_revision_"
                                f"{index:02d}_{proposal_index:02d}"
                            ),
                        )
                    )
                    repeated, repeated_metrics = await _run_candidate_semantic_gate(
                        candidates=[(proposal_index, candidate)],
                        fixed_definitions=fixed_definitions,
                        context=context,
                        settings=settings,
                        schema_name=(
                            f"field_discovery_semantic_gate_retry_"
                            f"{index:02d}_{proposal_index:02d}"
                        ),
                    )
                    decision = repeated[proposal_index]
                    semantic_rule_retry = {
                        "status": "completed",
                        "revision_metrics": retry_metrics,
                        "semantic_gate": repeated_metrics,
                    }
                except Exception as error:
                    semantic_rule_retry = {
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "error": (str(error).strip() or type(error).__name__)[:1200],
                    }

            if decision is None:
                semantic_attempts = repeated_metrics["candidates"][str(proposal_index)]
                reason = semantic_attempts["attempts"][-1]["error"]
                await logger.emit(
                    f"{prefix} [2/5 SEMANTIC GATE] 候选 {proposal_index} 规则修订后调用失败：{reason}"
                )
                candidate_records.append(
                    {
                        "proposal_index": proposal_index,
                        "field_id": candidate.definition.field_id,
                        "name": candidate.definition.name,
                        "status": "rejected_by_gate",
                        "rejection_category": "semantic_gate_error",
                        "reason": reason,
                        "gates": {
                            "structure_and_rule": item["structure_attempts"],
                            "semantic": semantic_attempts,
                            "semantic_rule_retry": semantic_rule_retry,
                        },
                    }
                )
                continue

            gates = {
                "structure_and_rule": item["structure_attempts"],
                "semantic": decision.model_dump(mode="json"),
                "semantic_rule_retry": semantic_rule_retry,
            }
            if decision.status != "accepted" or (
                semantic_rule_retry is not None and semantic_rule_retry["status"] == "failed"
            ):
                await logger.emit(
                    f"{prefix} [2/5 SEMANTIC GATE] 候选 {proposal_index} 拒绝："
                    f"{decision.status}｜{decision.reason}"
                )
                candidate_records.append(
                    {
                        "proposal_index": proposal_index,
                        "field_id": candidate.definition.field_id,
                        "name": candidate.definition.name,
                        "status": "rejected_by_gate",
                        "rejection_category": decision.status,
                        "reason": decision.reason,
                        "gates": gates,
                    }
                )
                continue
            accepted_candidates.append(
                {
                    "proposal_index": proposal_index,
                    "candidate": candidate,
                    "gates": gates,
                }
            )

        for item in accepted_candidates:
            proposal_index = item["proposal_index"]
            candidate = item["candidate"]
            gates = item["gates"]
            await logger.emit(
                f"{prefix} [2/5 SEMANTIC GATE] 候选 {proposal_index} 通过："
                f"{candidate.definition.field_id} / {candidate.definition.name}；"
                f"开始新候选池 Top-{top_k} 多路召回"
            )
            matches = await pool.top_matches(candidate, limit=top_k)
            rendered_matches = [
                {
                    "candidate_id": match.candidate_id,
                    "group_id": match.group_id,
                    "fused_score": round(match.fused_score, 8),
                    "best_rank": match.best_rank,
                    "view_scores": {
                        key: round(value, 8) for key, value in match.view_scores.items()
                    },
                    "view_ranks": match.view_ranks,
                }
                for match in matches
            ]
            if not matches:
                resolution = await resolve_candidate_identity(
                    proposal=candidate,
                    document_id=str(prepared["document_id"]),
                    matches=(),
                    comparisons={},
                    pool=pool,
                )
                await logger.emit(
                    f"{prefix} [5/5 POOL] {candidate.definition.field_id}：池为空，"
                    f"{resolution['action']} → {resolution['candidate_id']} / "
                    f"{resolution['group_id']}"
                )
                candidate_records.append(
                    {
                        "proposal_index": proposal_index,
                        "field_id": candidate.definition.field_id,
                        "name": candidate.definition.name,
                        "status": "accepted",
                        "gates": gates,
                        "novelty_reason": candidate.novelty_reason,
                        "evidence": {
                            "page_number": candidate.evidence_page_number,
                            "evidence_hash": candidate.evidence_hash,
                        },
                        "top_matches": [],
                        "comparisons": [],
                        "resolution": resolution,
                    }
                )
                continue

            await logger.emit(
                f"{prefix} [3/5 RETRIEVE] {candidate.definition.field_id} 融合召回 "
                + ", ".join(
                    f"{match['candidate_id']}@{match['fused_score']:.8f}"
                    for match in rendered_matches
                )
            )
            # 字段归属只比较两个定义的语义边界。候选已经通过 PDF 证据门禁，
            # 此处重复传合同图像既不增加判别事实，也会放大每个 Top 候选的调用成本。
            # 同一当前字段按 Top 顺序逐对执行，使稳定的“任务 + 当前字段”文本前缀可复用。
            relation_calls: list[dict[str, Any]] = []
            comparison_items: list[RelationComparison] = []
            for relation_index, match in enumerate(matches, start=1):
                comparison, call_attempts = await _judge_relation_with_retry(
                    proposal=candidate,
                    match=match,
                    pool=pool,
                    context=context,
                    settings=settings,
                    schema_name=(
                        f"field_discovery_relation_{index:02d}_{proposal_index:02d}_"
                        f"{relation_index:02d}"
                    ),
                )
                comparison_items.append(comparison)
                accepted_metrics = next(
                    attempt["metrics"]
                    for attempt in call_attempts
                    if attempt["status"] == "accepted"
                )
                relation_calls.append(
                    {
                        "target_candidate_id": match.candidate_id,
                        "attempts": call_attempts,
                        "metrics": accepted_metrics,
                    }
                )
                await logger.emit(
                    f"{prefix} [4/5 RELATION] {candidate.definition.field_id} "
                    f"Top-{relation_index}/{len(matches)} {match.candidate_id}="
                    f"{comparison.relation}（纯文本，尝试={len(call_attempts)}，"
                    f"耗时={accepted_metrics['elapsed_seconds']}s）"
                )

            comparisons = validate_relation_judgement(
                RelationJudgement(comparisons=comparison_items), matches
            )
            relation_metrics = {
                "call_count": len(relation_calls),
                "elapsed_seconds": round(
                    sum(float(item["metrics"]["elapsed_seconds"]) for item in relation_calls), 3
                ),
                "usage": {
                    key: sum(
                        int(item["metrics"].get("usage", {}).get(key) or 0)
                        for item in relation_calls
                    )
                    for key in sorted(
                        {
                            key
                            for item in relation_calls
                            for key in item["metrics"].get("usage", {})
                            if isinstance(item["metrics"].get("usage", {}).get(key), (int, float))
                        }
                    )
                },
                "image_count": 0,
            }
            await logger.emit(
                f"{prefix} [4/5 RELATION] {candidate.definition.field_id}："
                + ", ".join(
                    f"{match.candidate_id}={comparisons[match.candidate_id].relation}"
                    for match in matches
                )
            )
            resolution = await resolve_candidate_identity(
                proposal=candidate,
                document_id=str(prepared["document_id"]),
                matches=matches,
                comparisons=comparisons,
                pool=pool,
            )
            await logger.emit(
                f"{prefix} [5/5 POOL] {candidate.definition.field_id}：{resolution['action']} → "
                f"{resolution['candidate_id']} / {resolution['group_id']}，"
                f"判别耗时={relation_metrics['elapsed_seconds']}s"
            )
            candidate_records.append(
                {
                    "proposal_index": proposal_index,
                    "field_id": candidate.definition.field_id,
                    "name": candidate.definition.name,
                    "status": "accepted",
                    "gates": gates,
                    "novelty_reason": candidate.novelty_reason,
                    "evidence": {
                        "page_number": candidate.evidence_page_number,
                        "evidence_hash": candidate.evidence_hash,
                    },
                    "top_matches": rendered_matches,
                    "comparisons": [
                        comparisons[match.candidate_id].model_dump() for match in matches
                    ],
                    "relation_metrics": relation_metrics,
                    "relation_calls": relation_calls,
                    "resolution": resolution,
                }
            )

        return {
            "status": "succeeded",
            "source_name": pdf_path.name,
            "document_id": prepared["document_id"],
            "source_page_count": prepared["source_page_count"],
            "fixed_stage_summary": {
                "core_catalog_mode": pipelines.core_catalog_mode,
                "core_field_count": len(core_definitions),
                "attribute_catalog_mode": pipelines.attribute_catalog_mode,
                "attribute_field_count": len(attribute_definitions),
                "core_statuses": core_statuses.splitlines()[1:],
                "attribute_statuses": attribute_statuses.splitlines()[1:],
            },
            "discovery_metrics": discovery_metrics,
            "candidate_semantic_gate": semantic_gate_metrics,
            "candidates": sorted(candidate_records, key=lambda item: item["proposal_index"]),
            "candidate_pool_size_after_document": pool.size,
        }
    finally:
        await pipelines.close()


async def async_main(argv: Sequence[str] | None = None) -> tuple[Path, bool]:
    args = parse_args(argv)
    if args.max_members_per_group < 1:
        raise ValueError("--max-members-per-group 必须至少为 1。")
    # Embedding 在固定字段提取器之外单独建立客户端，因此在创建前显式加载同一环境文件。
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    settings = await load_project_settings(PROJECT_ROOT)
    input_dir = _resolve_project_path(args.input_dir)
    output_root = _resolve_project_path(args.output_dir)
    core_catalog_path = _resolve_project_path(
        args.core_catalog or settings.paths.discovery_core_fields
    )
    attribute_catalog_path = _resolve_project_path(
        args.attribute_catalog or settings.paths.discovery_attribute_fields
    )
    production_paths = {
        _resolve_project_path(settings.paths.core_fields).resolve(),
        _resolve_project_path(settings.paths.attribute_fields).resolve(),
    }
    if (
        core_catalog_path.resolve() in production_paths
        or attribute_catalog_path.resolve() in production_paths
    ):
        raise ValueError("字段发现实验必须使用独立目录，不能直接读取 production Core/Attribute YAML。")
    for catalog_path in (core_catalog_path, attribute_catalog_path):
        if not await run_blocking(catalog_path.is_file):
            raise FileNotFoundError(f"Discovery 字段目录不存在：{catalog_path}")
    # 字段目录属于批次级启动配置。先统一校验一次，避免同一个配置错误在每份合同上重复失败，
    # 更不能等到 PDF 渲染或模型调用后才暴露 status/fields 不一致。
    preflight_catalog = YamlFieldCatalog(
        core_path=core_catalog_path,
        attribute_path=attribute_catalog_path,
    )
    preflight_core, preflight_attribute = await asyncio.gather(
        preflight_catalog.snapshot(FieldKind.CORE),
        preflight_catalog.snapshot(FieldKind.ATTRIBUTE),
    )
    pdf_paths = await run_blocking(lambda: sorted(input_dir.glob("*.pdf")))
    if not pdf_paths:
        raise RuntimeError(f"合同目录中没有 PDF：{input_dir}")

    run_dir = output_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)
    logger = StageLogger(run_dir / "stage.log")
    await logger.emit("字段发现第一大步统一流水线启动")
    await logger.emit(f"输入合同数={len(pdf_paths)}；输入目录={input_dir}")
    await logger.emit(f"Discovery Core={core_catalog_path}")
    await logger.emit(f"Discovery Attribute={attribute_catalog_path}")
    await logger.emit(
        "字段目录启动校验通过："
        f"Core status={preflight_core.status} / fields={len(preflight_core.definitions)}；"
        f"Attribute status={preflight_attribute.status} / "
        f"fields={len(preflight_attribute.definitions)}"
    )

    policy = await load_contract_embedding_policy(
        _resolve_project_path(settings.paths.contract_embedding_policy)
    )
    embedding_settings = settings.models.embedding
    embedding_client = Qwen3VLEmbeddingClient(
        base_url=embedding_settings.base_url,
        api_key=os.getenv(embedding_settings.api_key_env) or "",
        model=embedding_settings.model,
        endpoint=embedding_settings.endpoint,
        timeout_seconds=embedding_settings.timeout_seconds,
        dimensions=embedding_settings.dimensions,
        max_concurrent_requests=embedding_settings.max_concurrent_requests,
        normalize=embedding_settings.normalize,
        policy=policy,
    )
    try:
        await embedding_client.probe()
        await logger.emit(
            f"Embedding 服务就绪：{embedding_client.model}，维度={embedding_client.dimensions}"
        )
        pool = CandidateVectorPool(embedding_client)
        cache_metrics_before = await _snapshot_vllm_cache_metrics(
            settings.models.mllm.base_url
        )
        manifest: dict[str, Any] = {
            "run_kind": "field_discovery_stage_one",
            "pipeline_mode": "semantic_admission_relation_graph_two_stage_refinement",
            "started_at": datetime.now(UTC).isoformat(),
            "pipeline_steps": [
                "extract_fixed_core_and_attribute",
                "discover_new_attribute_candidates",
                "semantic_candidate_admission",
                "retrieve_and_fuse_candidate_views",
                "judge_candidate_relation",
                "build_relation_graph_components",
                "plan_group_candidate_ownership",
                "compile_each_final_field",
                "run_global_semantic_gate",
            ],
            "input_dir": str(input_dir),
            "contracts": [path.name for path in pdf_paths],
            "discovery_core_catalog": {
                "path": str(core_catalog_path),
                "sha256": await run_blocking(_sha256_file_sync, core_catalog_path),
            },
            "discovery_attribute_catalog": {
                "path": str(attribute_catalog_path),
                "sha256": await run_blocking(_sha256_file_sync, attribute_catalog_path),
            },
            "model": settings.models.mllm.model,
            "embedding_model": embedding_client.model,
            "max_candidates_per_document": args.max_candidates_per_document,
            "top_k": args.top_k,
            "max_candidate_rule_retries": args.max_candidate_rule_retries,
            "max_members_per_group": args.max_members_per_group,
            "max_group_validation_retries": args.max_group_validation_retries,
            "vllm_cache_metrics": {"before": cache_metrics_before},
            "documents": [],
        }
        summary: list[dict[str, Any]] = []
        for index, pdf_path in enumerate(pdf_paths, start=1):
            try:
                result = await _process_document(
                    index=index,
                    total=len(pdf_paths),
                    pdf_path=pdf_path,
                    settings=settings,
                    core_catalog_path=core_catalog_path,
                    attribute_catalog_path=attribute_catalog_path,
                    pool=pool,
                    max_candidates=args.max_candidates_per_document,
                    top_k=args.top_k,
                    max_candidate_rule_retries=args.max_candidate_rule_retries,
                    logger=logger,
                )
                result_name = f"{index:02d}_document.json"
                await run_blocking(_write_json_sync, run_dir / result_name, result)
                summary.append(
                    # “模型提出”与“门禁接受”是不同口径，必须同时输出，避免把结构问题
                    # 误判为模型没有发现字段。
                    {
                        "index": index,
                        "source_name": pdf_path.name,
                        "status": "succeeded",
                        "result": result_name,
                        "model_candidate_count": len(result["candidates"]),
                        "accepted_candidate_count": sum(
                            item["status"] == "accepted" for item in result["candidates"]
                        ),
                        "rejected_candidate_count": sum(
                            item["status"] == "rejected_by_gate"
                            for item in result["candidates"]
                        ),
                        "candidate_pool_size_after_document": result[
                            "candidate_pool_size_after_document"
                        ],
                    }
                )
            except Exception as error:
                diagnostic_name = f"{index:02d}_failure_diagnostic.json"
                await run_blocking(
                    _write_json_sync,
                    run_dir / diagnostic_name,
                    {
                        "source_name": pdf_path.name,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                )
                summary.append(
                    {
                        "index": index,
                        "source_name": pdf_path.name,
                        "status": "failed",
                        "diagnostic": diagnostic_name,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                )
                await logger.emit(
                    f"[{index:02d}/{len(pdf_paths):02d}] {pdf_path.name} [FAILED] "
                    f"{type(error).__name__}: {error}"
                )
            manifest["documents"] = summary
            await run_blocking(_write_json_sync, run_dir / "summary.json", summary)
            await run_blocking(_write_json_sync, run_dir / "manifest.json", manifest)

        candidate_pool = pool.report()
        relation_graph = pool.relation_graph_report()
        candidate_pool_path = run_dir / "candidate_pool.json"
        await run_blocking(_write_json_sync, candidate_pool_path, candidate_pool)
        await run_blocking(
            _write_json_sync, run_dir / "candidate_relation_graph.json", relation_graph
        )
        failed = [item for item in summary if item["status"] == "failed"]
        manifest["final_candidate_identity_count"] = pool.size
        manifest["succeeded_document_count"] = len(summary) - len(failed)
        manifest["failed_document_count"] = len(failed)
        manifest["candidate_relation_edge_count"] = len(relation_graph["edges"])
        await run_blocking(_write_json_sync, run_dir / "manifest.json", manifest)

        # 候选池冻结后立即执行原独立实验的组级收敛，保证一条命令得到第二阶段所需字段草案。
        profiles = load_group_profiles(candidate_pool)
        await logger.emit(
            f"[5/5 CONSOLIDATE] 候选池冻结：身份={pool.size}，分组={len(profiles)}；"
            "开始组级字段收敛"
        )
        reports: list[dict[str, Any]]
        semantic_gate: dict[str, Any]
        if profiles:
            mllm = settings.models.mllm
            http_client = httpx.AsyncClient(timeout=mllm.timeout_seconds, trust_env=False)
            client = AsyncOpenAI(
                base_url=mllm.base_url,
                api_key=os.getenv(mllm.api_key_env) or "EMPTY",
                http_client=http_client,
            )
            limiter = ModelRequestLimiter(mllm.max_concurrent_requests)
            try:
                reports = await refine_candidate_groups(
                    profiles=profiles,
                    max_members_per_group=args.max_members_per_group,
                    max_validation_retries=args.max_group_validation_retries,
                    client=client,
                    settings=settings,
                    limiter=limiter,
                    emit=logger.emit,
                )
                preliminary_plan = build_refinement_plan(
                    profiles=profiles, reports=reports
                )
                semantic_gate = await run_global_semantic_gate(
                    final_fields=preliminary_plan["final_fields"],
                    fixed_definitions=(
                        *preflight_core.definitions,
                        *preflight_attribute.definitions,
                    ),
                    max_validation_retries=args.max_group_validation_retries,
                    client=client,
                    settings=settings,
                    limiter=limiter,
                )
            finally:
                await client.close()
                if not http_client.is_closed:
                    await http_client.aclose()
        else:
            reports = []
            semantic_gate = {
                "status": "passed",
                "decision_count": 0,
                "conflict_count": 0,
                "decisions": [],
                "attempts": [],
            }
            await logger.emit("[5/5 CONSOLIDATE] 本批次没有合格候选，稳定生成空字段草案")

        refinement_plan = build_refinement_plan(
            profiles=profiles, reports=reports, semantic_gate=semantic_gate
        )
        await run_blocking(
            _write_json_sync, run_dir / "group_refinements.json", reports
        )
        await run_blocking(
            _write_json_sync,
            run_dir / "field_definition_drafts.json",
            refinement_plan["final_fields"],
        )
        await run_blocking(
            _write_json_sync, run_dir / "refinement_plan.json", refinement_plan
        )
        await run_blocking(
            _write_json_sync, run_dir / "global_semantic_gate.json", semantic_gate
        )

        manifest.update(
            {
                "completed_at": datetime.now(UTC).isoformat(),
                "candidate_pool_sha256": await run_blocking(
                    _sha256_file_sync, candidate_pool_path
                ),
                "source_group_count": refinement_plan["source_group_count"],
                "succeeded_group_count": refinement_plan["succeeded_group_count"],
                "failed_group_count": refinement_plan["failed_group_count"],
                "final_field_count": refinement_plan["final_field_count"],
                "discarded_candidate_count": refinement_plan[
                    "discarded_candidate_count"
                ],
                "batch_field_id_gate": refinement_plan["batch_field_id_gate"],
                "batch_semantic_gate": refinement_plan["batch_semantic_gate"],
                "group_refinements": reports,
            }
        )
        manifest["vllm_cache_metrics"]["after"] = await _snapshot_vllm_cache_metrics(
            settings.models.mllm.base_url
        )
        pipeline_succeeded = (
            not failed
            and refinement_plan["failed_group_count"] == 0
            and refinement_plan["batch_field_id_gate"] == "passed"
            and refinement_plan["batch_semantic_gate"] == "passed"
        )
        manifest["status"] = "succeeded" if pipeline_succeeded else "failed"
        await run_blocking(_write_json_sync, run_dir / "manifest.json", manifest)
        await logger.emit(
            f"流水线结束：合同成功={len(summary) - len(failed)}，合同失败={len(failed)}，"
            f"候选身份={pool.size}，最终字段={refinement_plan['final_field_count']}，"
            f"失败分组={refinement_plan['failed_group_count']}，"
            f"全局语义门禁={refinement_plan['batch_semantic_gate']}，产物={run_dir}"
        )
        return run_dir, pipeline_succeeded
    finally:
        await embedding_client.close()


def main(argv: Sequence[str] | None = None) -> int:
    run_dir, succeeded = asyncio.run(async_main(argv))
    print(run_dir)
    return 0 if succeeded else 1


if __name__ == "__main__":
    # IDE 直接运行本文件（不配置运行参数）时，修改此处即可；路径均相对项目根目录。
    # 若 IDE Run Configuration 或终端实际传入了参数，则优先使用传入参数，不读取此编辑区。
    IDE_INPUT_DIR = "data/input"
    IDE_OUTPUT_DIR = "experiments/outputs/field_discovery_stage_one"
    IDE_CORE_CATALOG: str | None = None
    IDE_ATTRIBUTE_CATALOG: str | None = None
    IDE_MAX_CANDIDATES_PER_DOCUMENT = 5
    IDE_TOP_K = 5
    IDE_MAX_CANDIDATE_RULE_RETRIES = 1
    IDE_MAX_MEMBERS_PER_GROUP = 20
    IDE_MAX_GROUP_VALIDATION_RETRIES = 1

    supplied_argv = sys.argv[1:]
    if supplied_argv:
        raise SystemExit(main(supplied_argv))
    raise SystemExit(
        main(
            build_ide_argv(
                input_dir=IDE_INPUT_DIR,
                output_dir=IDE_OUTPUT_DIR,
                core_catalog=IDE_CORE_CATALOG,
                attribute_catalog=IDE_ATTRIBUTE_CATALOG,
                max_candidates_per_document=IDE_MAX_CANDIDATES_PER_DOCUMENT,
                top_k=IDE_TOP_K,
                max_candidate_rule_retries=IDE_MAX_CANDIDATE_RULE_RETRIES,
                max_members_per_group=IDE_MAX_MEMBERS_PER_GROUP,
                max_group_validation_retries=IDE_MAX_GROUP_VALIDATION_RETRIES,
            )
        )
    )
