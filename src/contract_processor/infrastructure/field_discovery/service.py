"""字段发现第一阶段正式服务：候选准入、身份归并与批次收敛。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI

from contract_processor.application.prompts.pdf_prefix import (
    build_common_rules,
    build_page_visibility_context,
)
from contract_processor.application.schemas.field_discovery import (
    FieldDiscoveryOutput,
    FieldDiscoveryRequest,
)
from contract_processor.async_utils import run_blocking
from contract_processor.domain.models import FieldDefinition
from contract_processor.infrastructure.embedding import Qwen3VLEmbeddingClient
from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.field_discovery.candidate_pipeline import (
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
    build_candidate_proposal_repair_prompt,
    build_discovery_prompt_after_images,
    build_discovery_prompt_before_images,
    build_extraction_rule_revision_prompt,
    build_relation_prompt,
    build_single_candidate_semantic_gate_prompt,
    invoke_candidate_proposals,
    invoke_structured,
    render_attribute_status_context,
    render_core_status_context,
    recover_candidate_semantic_gate_reference,
    resolve_candidate_identity,
    validate_candidate_proposal,
    validate_candidate_semantic_gate,
    validate_extraction_rule_revision,
    validate_relation_judgement,
    validate_single_relation_semantics,
)
from contract_processor.infrastructure.field_discovery.group_consolidation import (
    load_group_profiles,
)
from contract_processor.infrastructure.field_discovery.group_service import (
    build_refinement_plan,
    refine_candidate_groups,
    run_global_semantic_gate,
)
from contract_processor.infrastructure.field_discovery.prompt_templates import (
    render_discovery_prompt,
)
from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
from contract_processor.settings import ProjectSettings


EXTRACTION_RULE_REVISION_SYSTEM_MESSAGE = render_discovery_prompt(
    "00b_rule_revision_system.txt", {}
)
CANDIDATE_SEMANTIC_GATE_SYSTEM_MESSAGE = render_discovery_prompt(
    "00c_semantic_gate_system.txt", {}
)


def _safe_error(error: Exception) -> dict[str, Any]:
    """审计信息不保存模型原始输出或合同字段值。"""

    record: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error": (str(error).strip() or type(error).__name__)[:1200],
    }
    metrics = getattr(error, "metrics", None)
    if isinstance(metrics, dict):
        record["metrics"] = metrics
    return record


def validate_candidate_contract_repair(
    *, original: CandidateProposal, revised: CandidateProposal
) -> CandidateProposal:
    """锁定已解析候选的业务身份和证据，只允许修复输出契约与提取规则。"""

    mutable_fields = {"output", "extraction_rule"}
    original_locked = original.model_dump(
        mode="json", exclude=mutable_fields
    )
    revised_locked = revised.model_dump(
        mode="json", exclude=mutable_fields
    )
    if revised_locked != original_locked:
        changed = sorted(
            key
            for key in original_locked.keys() | revised_locked.keys()
            if original_locked.get(key) != revised_locked.get(key)
        )
        raise ValueError(
            "候选契约局部重试不得修改字段身份或证据："
            f"发生变化的字段={changed}。"
        )
    return revised


class StructuredFieldDiscoveryService:
    """在批次生命周期内共享候选向量池，并在批次末冻结最终字段。

    每份合同仍串行进入候选池，保证 candidate_id/group_id 可复现；同一合同的候选门禁、
    单个候选对应的 Top-K 关系判断均并发执行，实际请求数受共享限流器约束。
    """

    def __init__(
        self,
        *,
        project_root: Path,
        settings: ProjectSettings,
        embedding_client: Qwen3VLEmbeddingClient,
        max_candidates_per_document: int = 5,
        top_k: int = 5,
        max_candidate_rule_retries: int = 1,
        max_relation_validation_retries: int = 1,
        max_members_per_group: int = 20,
        max_group_validation_retries: int = 1,
    ) -> None:
        if not 1 <= max_candidates_per_document <= 5:
            raise ValueError("max_candidates_per_document 必须在 1..5 之间。")
        if not 1 <= top_k <= 5:
            raise ValueError("top_k 必须在 1..5 之间。")
        self._project_root = project_root
        self._settings = settings
        self._embedding_client = embedding_client
        self._pool = CandidateVectorPool(embedding_client)
        self._limiter = ModelRequestLimiter(settings.models.mllm.max_concurrent_requests)
        self._max_candidates = max_candidates_per_document
        self._top_k = top_k
        self._max_rule_retries = max_candidate_rule_retries
        self._max_relation_retries = max_relation_validation_retries
        self._max_members_per_group = max_members_per_group
        self._max_group_retries = max_group_validation_retries
        self._fixed_definitions: tuple[FieldDefinition, ...] | None = None
        self._consolidation: dict[str, Any] | None = None
        self._embedding_ready = False
        self._embedding_probe_lock = asyncio.Lock()
        self._closed = False

    @property
    def max_candidates_per_document(self) -> int:
        return self._max_candidates

    @property
    def top_k(self) -> int:
        return self._top_k

    async def discover(self, request: FieldDiscoveryRequest) -> FieldDiscoveryOutput:
        """处理一份合同，并把已准入候选解析为批次内稳定身份。"""

        if self._closed:
            raise RuntimeError("字段发现服务已经关闭。")
        if self._consolidation is not None:
            raise RuntimeError("候选池冻结后不能继续加入新合同。")
        await self._ensure_embedding_ready()
        fixed_definitions = (*request.core_definitions, *request.attribute_definitions)
        self._bind_fixed_catalog(fixed_definitions)
        client, http_client = await self._open_mllm_client()
        context = PdfExtractionContext(
            project_root=self._project_root,
            pdf_path=request.contract_path,
            document_id=request.document_id,
            images=[
                {
                    "page": page.page_number,
                    "data_url": page.data_url,
                    "image_bytes": page.image_bytes,
                }
                for page in request.pages
            ],
            source_page_count=len(request.pages),
            client=client,
            model_request_limiter=self._limiter,
        )
        try:
            common_rules = await build_common_rules()
            parsed, parse_failures, proposal_metrics = await invoke_candidate_proposals(
                client=client,
                context=context,
                settings=self._settings,
                pre_image_prompt=common_rules
                + "\n\n"
                + build_discovery_prompt_before_images(
                    core_definitions=request.core_definitions,
                    attribute_definitions=request.attribute_definitions,
                    max_candidates=self._max_candidates,
                ),
                post_image_prompt=build_discovery_prompt_after_images(
                    core_status_context=render_core_status_context(
                        request.core_result, request.core_definitions
                    ),
                    attribute_status_context=render_attribute_status_context(
                        request.attribute_result, request.attribute_definitions
                    ),
                    page_visibility_context=build_page_visibility_context(
                        len(request.pages)
                    ),
                ),
                schema_name="field_discovery_candidates",
                max_completion_tokens=6144,
            )
            repaired = await asyncio.gather(
                *(
                    self._repair_invalid_candidate(
                        failure.payload,
                        failure.error,
                        context,
                        schema_name=f"candidate_repair_{failure.proposal_index:02d}",
                    )
                    for failure in parse_failures
                )
            )
            generated = list(parsed)
            rejected_records: list[dict[str, Any]] = []
            for failure, (candidate, audit) in zip(
                parse_failures, repaired, strict=True
            ):
                if candidate is not None:
                    generated.append((failure.proposal_index, candidate))
                else:
                    rejected_records.append(
                        {
                            "proposal_index": failure.proposal_index,
                            "status": "rejected",
                            "failed_stage": "proposal_schema_repair",
                            "reason": failure.error,
                            "audit": audit,
                        }
                    )

            admission_results = await asyncio.gather(
                *(
                    self._admit_candidate(
                        proposal_index=index,
                        proposal=proposal,
                        fixed_definitions=fixed_definitions,
                        context=context,
                    )
                    for index, proposal in sorted(generated, key=lambda item: item[0])
                )
            )
            admitted: list[tuple[int, CandidateProposalRecord, dict[str, Any]]] = []
            for result in admission_results:
                if result[1] is None:
                    rejected_records.append(result[2])
                else:
                    admitted.append((result[0], result[1], result[2]))

            accepted_records: list[dict[str, Any]] = []
            # 候选依次更新共享池，确保后一个候选能够召回本合同更早建立的身份。
            for proposal_index, candidate, admission_audit in admitted:
                resolved = await self._resolve_candidate(
                    proposal_index=proposal_index,
                    candidate=candidate,
                    document_id=request.document_id,
                    context=context,
                )
                if resolved["status"] != "accepted":
                    rejected_records.append(
                        {
                            "proposal_index": proposal_index,
                            "field_id": candidate.definition.field_id,
                            "name": candidate.definition.name,
                            "status": "rejected",
                            "failed_stage": "relation_judgement",
                            "reason": resolved["reason"],
                            "admission": admission_audit,
                            "relation": resolved,
                        }
                    )
                    continue
                accepted_records.append(
                    {
                        **candidate.definition_record,
                        "discovery": {
                            "proposal_index": proposal_index,
                            "field_id": candidate.definition.field_id,
                            "name": candidate.definition.name,
                            "status": "accepted",
                            "novelty_reason": candidate.novelty_reason,
                            "evidence": {
                                "page_number": candidate.evidence_page_number,
                                "evidence_hash": candidate.evidence_hash,
                            },
                            "admission": admission_audit,
                            "top_matches": resolved["top_matches"],
                            "comparisons": resolved["comparisons"],
                            "resolution": resolved["resolution"],
                        },
                    }
                )
            all_records = sorted(
                [
                    *(item["discovery"] for item in accepted_records),
                    *rejected_records,
                ],
                key=lambda item: int(item["proposal_index"]),
            )
            return FieldDiscoveryOutput(
                candidates=tuple(accepted_records),
                metrics={
                    "model_candidate_count": len(parsed) + len(parse_failures),
                    "valid_candidate_count_before_repair": len(parsed),
                    "repaired_candidate_count": sum(
                        candidate is not None for candidate, _ in repaired
                    ),
                    "accepted_candidate_count": len(accepted_records),
                    "rejected_candidate_count": len(rejected_records),
                    "candidate_pool_identity_count": self._pool.size,
                    "proposal": proposal_metrics,
                    "candidates": all_records,
                },
            )
        finally:
            await client.close()
            if not http_client.is_closed:
                await http_client.aclose()

    async def consolidate(self) -> dict[str, Any]:
        """冻结候选池，执行并发分组收敛和逐字段全局语义门禁。"""

        if self._consolidation is not None:
            return self._consolidation
        fixed_definitions = self._fixed_definitions or ()
        candidate_pool = self._pool.report()
        relation_graph = self._pool.relation_graph_report()
        profiles = load_group_profiles(candidate_pool)
        reports: list[dict[str, Any]] = []
        if profiles:
            client, http_client = await self._open_mllm_client()

            async def ignore_log(_message: str) -> None:
                return None

            try:
                reports = await refine_candidate_groups(
                    profiles=profiles,
                    max_members_per_group=self._max_members_per_group,
                    max_validation_retries=self._max_group_retries,
                    client=client,
                    settings=self._settings,
                    limiter=self._limiter,
                    emit=ignore_log,
                )
                preliminary = build_refinement_plan(profiles=profiles, reports=reports)
                semantic_gate = await run_global_semantic_gate(
                    final_fields=preliminary["final_fields"],
                    fixed_definitions=fixed_definitions,
                    max_validation_retries=self._max_group_retries,
                    client=client,
                    settings=self._settings,
                    limiter=self._limiter,
                )
            finally:
                await client.close()
                if not http_client.is_closed:
                    await http_client.aclose()
        else:
            semantic_gate = {
                "status": "passed",
                "decision_count": 0,
                "conflict_count": 0,
                "failed_field_count": 0,
                "failed_field_refs": [],
                "decisions": [],
                "field_results": [],
                "attempts": [],
            }
        plan = build_refinement_plan(
            profiles=profiles, reports=reports, semantic_gate=semantic_gate
        )
        accepted_refs = {
            item["final_field_ref"]
            for item in semantic_gate.get("decisions", [])
            if item.get("status") == "accepted"
        }
        frozen_fields = [
            {
                "candidate_ref": f"{item['group_id']}:{item['definition']['field_id']}",
                "group_id": item["group_id"],
                "source_candidate_ids": item.get("source_candidate_ids", []),
                "definition": item["definition"],
            }
            for item in plan["final_fields"]
            if f"{item['group_id']}:{item['definition']['field_id']}" in accepted_refs
        ]
        if plan["batch_field_id_gate"] != "passed":
            # 跨组 field_id 冲突时，第二阶段无法用同一 Schema 键安全区分字段身份。
            frozen_fields = []
        has_failure = (
            plan["failed_group_count"] > 0
            or plan["partially_succeeded_group_count"] > 0
            or plan["batch_field_id_gate"] != "passed"
            or plan["batch_semantic_gate"] != "passed"
        )
        self._consolidation = {
            "status": "completed_with_failures" if has_failure else "completed",
            "candidate_identity_count": self._pool.size,
            "source_group_count": plan["source_group_count"],
            "succeeded_group_count": plan["succeeded_group_count"],
            "partially_succeeded_group_count": plan[
                "partially_succeeded_group_count"
            ],
            "failed_group_count": plan["failed_group_count"],
            "final_field_count": plan["final_field_count"],
            "frozen_field_count": len(frozen_fields),
            "discarded_candidate_count": plan["discarded_candidate_count"],
            "batch_field_id_gate": plan["batch_field_id_gate"],
            "batch_semantic_gate": plan["batch_semantic_gate"],
            "candidate_pool": candidate_pool,
            "relation_graph": relation_graph,
            "group_refinements": reports,
            "global_semantic_gate": semantic_gate,
            "frozen_fields": frozen_fields,
        }
        return self._consolidation

    async def close(self) -> None:
        """释放批次级 Embedding 连接。"""

        if not self._closed:
            await self._embedding_client.close()
            self._closed = True

    def _bind_fixed_catalog(
        self, fixed_definitions: Sequence[FieldDefinition]
    ) -> None:
        current = tuple(fixed_definitions)
        if self._fixed_definitions is None:
            self._fixed_definitions = current
            return
        expected = tuple(item.field_id for item in self._fixed_definitions)
        actual = tuple(item.field_id for item in current)
        if actual != expected:
            raise RuntimeError(
                "同一 discovery 批次的固定 Core/Attribute 目录发生变化："
                f"期望={expected}，实际={actual}。"
            )

    async def _open_mllm_client(self) -> tuple[AsyncOpenAI, httpx.AsyncClient]:
        await run_blocking(load_dotenv, self._project_root / ".env")
        mllm = self._settings.models.mllm
        http_client = httpx.AsyncClient(timeout=mllm.timeout_seconds, trust_env=False)
        client = AsyncOpenAI(
            base_url=mllm.base_url,
            api_key=os.getenv(mllm.api_key_env) or "EMPTY",
            http_client=http_client,
        )
        return client, http_client

    async def _ensure_embedding_ready(self) -> None:
        """批次首次处理前探活，避免完成视觉发现后才暴露向量服务配置错误。"""

        if self._embedding_ready:
            return
        async with self._embedding_probe_lock:
            if self._embedding_ready:
                return
            await self._embedding_client.probe()
            self._embedding_ready = True

    async def _repair_invalid_candidate(
        self,
        payload: object,
        validation_error: str,
        context: PdfExtractionContext,
        *,
        schema_name: str,
        lock_identity: bool = False,
    ) -> tuple[CandidateProposal | None, dict[str, Any]]:
        try:
            revised, metrics = await invoke_structured(
                client=context.client,
                context=context,
                settings=self._settings,
                pre_image_prompt=build_candidate_proposal_repair_prompt(
                    proposal_payload=payload,
                    validation_error=validation_error,
                    lock_identity=lock_identity,
                ),
                post_image_prompt="请输出修复后的单个候选字段 JSON。",
                schema_model=CandidateProposal,
                schema_name=schema_name,
                max_completion_tokens=4096,
                include_images=False,
            )
        except Exception as error:
            return None, {"status": "failed", **_safe_error(error)}
        assert isinstance(revised, CandidateProposal)
        return revised, {"status": "accepted", "metrics": metrics}

    async def _admit_candidate(
        self,
        *,
        proposal_index: int,
        proposal: CandidateProposal,
        fixed_definitions: Sequence[FieldDefinition],
        context: PdfExtractionContext,
    ) -> tuple[int, CandidateProposalRecord | None, dict[str, Any]]:
        """单候选独立完成结构、规则和语义准入，并局部修复一次契约错误。"""

        current = proposal
        structure_attempts: list[dict[str, Any]] = []
        for attempt in range(self._max_rule_retries + 1):
            try:
                candidate = validate_candidate_proposal(
                    current,
                    fixed_definitions=fixed_definitions,
                    source_page_count=context.source_page_count,
                )
                structure_attempts.append(
                    {"attempt": attempt + 1, "status": "accepted"}
                )
                break
            except ExtractionRuleGeneralizationError as error:
                structure_attempts.append(
                    {
                        "attempt": attempt + 1,
                        "status": "rejected",
                        "error": str(error),
                    }
                )
                if attempt >= self._max_rule_retries:
                    return proposal_index, None, self._rejection(
                        proposal_index, proposal, "structure_or_rule", str(error),
                        {"structure": structure_attempts}
                    )
                try:
                    revised, metrics = await invoke_structured(
                        client=context.client,
                        context=context,
                        settings=self._settings,
                        pre_image_prompt=build_extraction_rule_revision_prompt(
                            proposal=current, validation_error=str(error)
                        ),
                        post_image_prompt="请输出修订后的通用 extraction_rule。",
                        schema_model=ExtractionRuleRevision,
                        schema_name=f"candidate_rule_revision_{proposal_index:02d}",
                        max_completion_tokens=2048,
                        include_images=False,
                        system_message=EXTRACTION_RULE_REVISION_SYSTEM_MESSAGE,
                    )
                    assert isinstance(revised, ExtractionRuleRevision)
                    revised_output = validate_extraction_rule_revision(
                        original_output=current.output,
                        revised_output=revised.output,
                    )
                    current = current.model_copy(
                        update={
                            "extraction_rule": revised.extraction_rule,
                            "output": revised_output,
                        }
                    )
                    structure_attempts[-1]["retry_metrics"] = metrics
                except Exception as retry_error:
                    structure_attempts.append(
                        {
                            "attempt": attempt + 2,
                            "status": "retry_failed",
                            **_safe_error(retry_error),
                        }
                    )
                    return proposal_index, None, self._rejection(
                        proposal_index,
                        proposal,
                        "structure_or_rule",
                        str(retry_error),
                        {"structure": structure_attempts},
                    )
            except ValueError as error:
                structure_attempts.append(
                    {
                        "attempt": attempt + 1,
                        "status": "rejected",
                        "error": str(error),
                    }
                )
                if attempt >= self._max_rule_retries:
                    return proposal_index, None, self._rejection(
                        proposal_index,
                        proposal,
                        "structure_or_rule",
                        str(error),
                        {"structure": structure_attempts},
                    )
                revised, repair_audit = await self._repair_invalid_candidate(
                    current.model_dump(mode="json"),
                    str(error),
                    context,
                    schema_name=f"candidate_contract_repair_{proposal_index:02d}",
                    lock_identity=True,
                )
                structure_attempts[-1]["retry"] = repair_audit
                if revised is None:
                    structure_attempts.append(
                        {
                            "attempt": attempt + 2,
                            "status": "retry_failed",
                            "error": "候选契约局部重试未返回合法 CandidateProposal。",
                        }
                    )
                    return proposal_index, None, self._rejection(
                        proposal_index,
                        proposal,
                        "structure_or_rule",
                        "候选契约局部重试未返回合法 CandidateProposal。",
                        {"structure": structure_attempts},
                    )
                try:
                    current = validate_candidate_contract_repair(
                        original=current, revised=revised
                    )
                except ValueError as retry_error:
                    structure_attempts.append(
                        {
                            "attempt": attempt + 2,
                            "status": "retry_failed",
                            **_safe_error(retry_error),
                        }
                    )
                    return proposal_index, None, self._rejection(
                        proposal_index,
                        proposal,
                        "structure_or_rule",
                        str(retry_error),
                        {"structure": structure_attempts},
                    )
        else:  # pragma: no cover - 循环的成功和失败分支均显式返回。
            raise AssertionError("候选结构门禁未产生结果。")

        decision, semantic_attempts = await self._judge_semantic_admission(
            proposal_index=proposal_index,
            candidate=candidate,
            fixed_definitions=fixed_definitions,
            context=context,
            schema_suffix="",
        )
        semantic_rule_retry: dict[str, Any] | None = None
        if decision is not None and decision.status == "invalid_rule":
            try:
                revised, revision_metrics = await invoke_structured(
                    client=context.client,
                    context=context,
                    settings=self._settings,
                    pre_image_prompt=build_extraction_rule_revision_prompt(
                        proposal=current, validation_error=decision.reason
                    ),
                    post_image_prompt="请输出与当前字段语义一致的通用 extraction_rule。",
                    schema_model=ExtractionRuleRevision,
                    schema_name=f"semantic_rule_revision_{proposal_index:02d}",
                    max_completion_tokens=2048,
                    include_images=False,
                    system_message=EXTRACTION_RULE_REVISION_SYSTEM_MESSAGE,
                )
                assert isinstance(revised, ExtractionRuleRevision)
                revised_output = validate_extraction_rule_revision(
                    original_output=current.output, revised_output=revised.output
                )
                current = current.model_copy(
                    update={
                        "extraction_rule": revised.extraction_rule,
                        "output": revised_output,
                    }
                )
                candidate = validate_candidate_proposal(
                    current,
                    fixed_definitions=fixed_definitions,
                    source_page_count=context.source_page_count,
                )
                decision, retry_attempts = await self._judge_semantic_admission(
                    proposal_index=proposal_index,
                    candidate=candidate,
                    fixed_definitions=fixed_definitions,
                    context=context,
                    schema_suffix="_retry",
                )
                semantic_rule_retry = {
                    "status": "completed",
                    "revision_metrics": revision_metrics,
                    "semantic_attempts": retry_attempts,
                }
            except Exception as error:
                semantic_rule_retry = {"status": "failed", **_safe_error(error)}

        audit = {
            "structure": structure_attempts,
            "semantic": semantic_attempts,
            "semantic_rule_retry": semantic_rule_retry,
        }
        if decision is None:
            return proposal_index, None, self._rejection(
                proposal_index, proposal, "semantic_gate_error",
                "单候选语义准入在一次纠错后仍未返回合法结论。", audit
            )
        if decision.status != "accepted" or (
            semantic_rule_retry is not None
            and semantic_rule_retry.get("status") == "failed"
        ):
            return proposal_index, None, self._rejection(
                proposal_index, proposal, decision.status, decision.reason, audit
            )
        return proposal_index, candidate, {
            **audit,
            "decision": decision.model_dump(mode="json"),
        }

    async def _judge_semantic_admission(
        self,
        *,
        proposal_index: int,
        candidate: CandidateProposalRecord,
        fixed_definitions: Sequence[FieldDefinition],
        context: PdfExtractionContext,
        schema_suffix: str,
    ) -> tuple[CandidateSemanticGateDecision | None, list[dict[str, Any]]]:
        prompt = build_single_candidate_semantic_gate_prompt(
            candidate_index=proposal_index,
            candidate=candidate,
            fixed_definitions=fixed_definitions,
        )
        attempts: list[dict[str, Any]] = []
        correction: str | None = None
        for attempt in range(2):
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
                    settings=self._settings,
                    pre_image_prompt=current_prompt,
                    post_image_prompt="请输出当前候选的语义准入 JSON。",
                    schema_model=CandidateSemanticGateDecision,
                    schema_name=(
                        f"candidate_semantic_gate_{proposal_index:02d}{schema_suffix}"
                    ),
                    max_completion_tokens=2048,
                    include_images=False,
                    system_message=CANDIDATE_SEMANTIC_GATE_SYSTEM_MESSAGE,
                )
                assert isinstance(response, CandidateSemanticGateDecision)
                raw_covered_by_field_id = response.covered_by_field_id
                response = recover_candidate_semantic_gate_reference(
                    response, fixed_definitions=fixed_definitions
                )
                decision = validate_candidate_semantic_gate(
                    CandidateSemanticGateBatch(decisions=[response]),
                    expected_indices=[proposal_index],
                    fixed_definitions=fixed_definitions,
                )[proposal_index]
            except Exception as error:
                correction = str(error)
                attempts.append(
                    {"attempt": attempt + 1, "status": "rejected", **_safe_error(error)}
                )
                continue
            accepted_attempt: dict[str, Any] = {
                "attempt": attempt + 1,
                "status": "accepted",
                "metrics": metrics,
            }
            if (
                raw_covered_by_field_id is None
                and response.covered_by_field_id is not None
            ):
                accepted_attempt["recovered_covered_by_field_id"] = (
                    response.covered_by_field_id
                )
            attempts.append(accepted_attempt)
            return decision, attempts
        return None, attempts

    async def _resolve_candidate(
        self,
        *,
        proposal_index: int,
        candidate: CandidateProposalRecord,
        document_id: str,
        context: PdfExtractionContext,
    ) -> dict[str, Any]:
        matches = await self._pool.top_matches(candidate, limit=self._top_k)
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
                document_id=document_id,
                matches=(),
                comparisons={},
                pool=self._pool,
            )
            return {
                "status": "accepted",
                "top_matches": [],
                "comparisons": [],
                "resolution": resolution,
            }

        # 每个 Top-K 字段对是独立纯文本任务；并发失败也只拒绝当前新候选。
        results = await asyncio.gather(
            *(
                self._judge_relation(
                    candidate=candidate,
                    target=match,
                    context=context,
                    schema_name=(
                        f"candidate_relation_{proposal_index:02d}_{index:02d}"
                    ),
                )
                for index, match in enumerate(matches, start=1)
            ),
            return_exceptions=True,
        )
        errors = [item for item in results if isinstance(item, BaseException)]
        if errors:
            return {
                "status": "rejected",
                "reason": "Top-K 关系判定存在失败项，无法安全确定候选身份。",
                "top_matches": rendered_matches,
                "errors": [
                    _safe_error(error) for error in errors if isinstance(error, Exception)
                ],
            }
        relation_items = [item for item in results if not isinstance(item, BaseException)]
        comparisons = validate_relation_judgement(
            RelationJudgement(comparisons=[item[0] for item in relation_items]),
            matches,
        )
        resolution = await resolve_candidate_identity(
            proposal=candidate,
            document_id=document_id,
            matches=matches,
            comparisons=comparisons,
            pool=self._pool,
        )
        return {
            "status": "accepted",
            "top_matches": rendered_matches,
            "comparisons": [
                {
                    **comparisons[match.candidate_id].model_dump(mode="json"),
                    "attempts": relation_items[index][1],
                }
                for index, match in enumerate(matches)
            ],
            "resolution": resolution,
        }

    async def _judge_relation(
        self,
        *,
        candidate: CandidateProposalRecord,
        target: Any,
        context: PdfExtractionContext,
        schema_name: str,
    ) -> tuple[RelationComparison, list[dict[str, Any]]]:
        prompt = build_relation_prompt(
            proposal=candidate, match=target, pool=self._pool
        )
        attempts: list[dict[str, Any]] = []
        correction: str | None = None
        for attempt in range(self._max_relation_retries + 1):
            target_prompt = prompt.target
            if correction:
                target_prompt += (
                    "\n\n【上次判定的程序校验失败】\n"
                    + correction
                    + "\n请重新比较两个顶层字段的完整业务边界。"
                )
            try:
                response, metrics = await invoke_structured(
                    client=context.client,
                    context=context,
                    settings=self._settings,
                    pre_image_prompt=prompt.preamble,
                    post_image_prompt=target_prompt,
                    schema_model=SingleRelationJudgement,
                    schema_name=schema_name,
                    max_completion_tokens=4096,
                    include_images=False,
                    system_message=FIELD_RELATION_SYSTEM_MESSAGE,
                )
                assert isinstance(response, SingleRelationJudgement)
                response = validate_single_relation_semantics(
                    proposal=candidate,
                    target=self._pool.identity(target.candidate_id).proposal,
                    judgement=response,
                )
            except Exception as error:
                correction = str(error)
                attempts.append(
                    {"attempt": attempt + 1, "status": "rejected", **_safe_error(error)}
                )
                continue
            attempts.append(
                {"attempt": attempt + 1, "status": "accepted", "metrics": metrics}
            )
            return (
                RelationComparison(
                    target_candidate_id=target.candidate_id,
                    reason=response.reason,
                    relation=response.relation,
                ),
                attempts,
            )
        raise RuntimeError(
            f"候选关系 {candidate.definition.field_id} -> {target.candidate_id} "
            "在一次纠错后仍无有效结果。"
        )

    @staticmethod
    def _rejection(
        proposal_index: int,
        proposal: CandidateProposal,
        failed_stage: str,
        reason: str,
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "proposal_index": proposal_index,
            "field_id": proposal.field_id,
            "name": proposal.name,
            "status": "rejected",
            "failed_stage": failed_stage,
            "reason": reason,
            "audit": audit,
        }
