"""可由统一字段发现流水线和历史复现实验共同调用的组级收敛服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import time
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from contract_processor.domain.models import FieldDefinition
from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
from contract_processor.settings import ProjectSettings
from experiments.field_discovery_stage_one.field_description import field_definition_record
from experiments.field_discovery_group_consolidation.merger import (
    FinalFieldDefinitionSuggestion,
    GlobalConflictConfirmation,
    GlobalSemanticDecision,
    GroupOwnershipPlan,
    GroupProfile,
    OwnershipFieldPlan,
    SingleGlobalSemanticDecision,
    build_final_field_definition_prompt,
    build_global_conflict_confirmation_prompt,
    build_global_semantic_gate_prompt,
    build_group_ownership_prompt,
    finalize_singleton_group,
    validate_batch_field_ids,
    validate_final_field_definition,
    validate_group_ownership_plan,
    validate_single_global_semantic_decision,
)


GROUP_REFINEMENT_SYSTEM_MESSAGE = (
    "你是合同元数据治理助手。你只能依据给定候选字段定义完成组内字段收敛，"
    "不得虚构合同内容、不得输出字段具体值、不得修改来源候选。"
)
GLOBAL_GATE_SYSTEM_MESSAGE = (
    "你是合同元数据字段库的最终语义门禁。你只报告固定覆盖、跨组重复或边界重叠，"
    "不得改写字段，也不得虚构合同事实。"
)
LogEmitter = Callable[[str], Awaitable[None]]
SchemaModel = TypeVar("SchemaModel", bound=BaseModel)


class StructuredInvocationError(RuntimeError):
    """携带非敏感请求指标的结构化响应错误，不保存模型原文。"""

    def __init__(self, message: str, *, metrics: dict[str, Any]) -> None:
        super().__init__(message)
        self.metrics = metrics


def _safe_error(error: Exception) -> dict[str, Any]:
    """实验只保存可行动的错误类型与路径，不落模型原始响应。"""

    message = str(error).strip() or type(error).__name__
    record: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error": message[:1200],
    }
    if isinstance(error, StructuredInvocationError):
        record["metrics"] = error.metrics
    return record


async def _invoke_json(
    *,
    prompt: str,
    correction: str | None,
    schema_model: type[SchemaModel],
    schema_name: str,
    phase_label: str,
    client: AsyncOpenAI,
    settings: ProjectSettings,
    limiter: ModelRequestLimiter,
    system_message: str = GROUP_REFINEMENT_SYSTEM_MESSAGE,
    max_completion_tokens: int = 8192,
) -> tuple[SchemaModel, dict[str, Any]]:
    """统一执行强 Schema 调用；解析失败交给调用方按相同语义任务重试一次。"""

    if correction:
        prompt += (
            "\n\n【上次输出的程序校验失败】\n"
            + correction
            + "\n请只修正该问题并重新输出完整 JSON。"
        )
    generation = settings.models.mllm.generation
    started = time.perf_counter()
    async with limiter.slot():
        completion = await client.chat.completions.create(
            model=settings.models.mllm.model,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema_model.model_json_schema(),
                },
            },
            max_completion_tokens=min(generation.max_completion_tokens, max_completion_tokens),
            temperature=generation.temperature,
            top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            extra_body={
                "top_k": generation.top_k,
                "repetition_penalty": generation.repetition_penalty,
                "seed": generation.seed,
            },
        )
    metrics = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "finish_reason": completion.choices[0].finish_reason,
        "image_count": 0,
    }
    content = completion.choices[0].message.content
    if not content:
        raise StructuredInvocationError(
            f"{phase_label}模型未返回结构化 JSON。", metrics=metrics
        )
    try:
        return schema_model.model_validate_json(content), metrics
    except Exception as error:
        raise StructuredInvocationError(
            f"{phase_label}模型输出未通过 JSON Schema。", metrics=metrics
        ) from error


async def _plan_ownership(
    *,
    profile: GroupProfile,
    max_members_per_group: int,
    max_validation_retries: int,
    client: AsyncOpenAI,
    settings: ProjectSettings,
    limiter: ModelRequestLimiter,
) -> tuple[GroupOwnershipPlan | None, dict[str, Any] | None, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    correction: str | None = None
    prompt = build_group_ownership_prompt(
        profile=profile, max_members_per_group=max_members_per_group
    )
    for attempt in range(max_validation_retries + 1):
        metrics: dict[str, Any] | None = None
        try:
            response, metrics = await _invoke_json(
                prompt=prompt,
                correction=correction,
                schema_model=GroupOwnershipPlan,
                schema_name="field_discovery_group_ownership",
                phase_label="候选去向规划",
                client=client,
                settings=settings,
                limiter=limiter,
                max_completion_tokens=4096,
            )
            report = validate_group_ownership_plan(response=response, profile=profile)
        except Exception as error:
            error_record = _safe_error(error)
            if metrics is not None and "metrics" not in error_record:
                error_record["metrics"] = metrics
            attempts.append(
                {"attempt": attempt + 1, "status": "rejected", **error_record}
            )
            required_ids = "、".join(
                member.candidate_id for member in profile.members
            )
            correction = (
                str(error_record["error"])
                + "\n必须逐一处置这些 candidate_id："
                + required_ids
                + "。不得让 final_field_plans 与 discarded_candidates 同时为空；"
                "不确定时为该候选建立独立 plan。"
            )
            continue
        attempts.append(
            {"attempt": attempt + 1, "status": "accepted", "metrics": metrics}
        )
        return response, report, attempts
    return None, None, attempts


async def _define_final_field(
    *,
    profile: GroupProfile,
    plan: OwnershipFieldPlan,
    sibling_plans: Sequence[OwnershipFieldPlan],
    max_validation_retries: int,
    client: AsyncOpenAI,
    settings: ProjectSettings,
    limiter: ModelRequestLimiter,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    correction: str | None = None
    prompt = build_final_field_definition_prompt(
        profile=profile, plan=plan, sibling_plans=sibling_plans
    )
    for attempt in range(max_validation_retries + 1):
        metrics: dict[str, Any] | None = None
        try:
            response, metrics = await _invoke_json(
                prompt=prompt,
                correction=correction,
                schema_model=FinalFieldDefinitionSuggestion,
                schema_name=f"field_discovery_definition_{plan.plan_id}",
                phase_label=f"最终字段 {plan.plan_id} 定义",
                client=client,
                settings=settings,
                limiter=limiter,
                max_completion_tokens=6144,
            )
            field = validate_final_field_definition(
                suggestion=response,
                plan=plan,
                sibling_plans=sibling_plans,
                profile=profile,
            )
        except Exception as error:
            error_record = _safe_error(error)
            if metrics is not None and "metrics" not in error_record:
                error_record["metrics"] = metrics
            attempts.append(
                {"attempt": attempt + 1, "status": "rejected", **error_record}
            )
            correction = error_record["error"]
            continue
        attempts.append(
            {"attempt": attempt + 1, "status": "accepted", "metrics": metrics}
        )
        return field.report(), {
            "plan_id": plan.plan_id,
            "status": "succeeded",
            "attempts": attempts,
        }
    return None, {
        "plan_id": plan.plan_id,
        "status": "failed",
        "attempts": attempts,
    }


async def refine_group(
    *,
    profile: GroupProfile,
    max_members_per_group: int,
    max_validation_retries: int,
    client: AsyncOpenAI,
    settings: ProjectSettings,
    limiter: ModelRequestLimiter,
) -> dict[str, Any]:
    """单候选确定性直通；多候选先规划唯一去向，再逐字段生成并编译定义。"""

    if len(profile.members) == 1:
        try:
            report = finalize_singleton_group(profile)
        except Exception as error:
            return {
                "group_id": profile.group_id,
                "status": "failed",
                "failed_stage": "singleton_validation",
                "source_member_count": 1,
                "stages": {
                    "ownership": {
                        "status": "deterministic_passthrough",
                        "attempts": [],
                    },
                    "definitions": [
                        {
                            "plan_id": "singleton",
                            "status": "failed",
                            **_safe_error(error),
                        }
                    ],
                },
            }
        report.update(
            {
                "status": "succeeded",
                "source_member_count": 1,
                "stages": {
                    "ownership": {"status": "deterministic_passthrough", "attempts": []},
                    "definitions": [],
                },
            }
        )
        return report

    response, ownership_report, ownership_attempts = await _plan_ownership(
        profile=profile,
        max_members_per_group=max_members_per_group,
        max_validation_retries=max_validation_retries,
        client=client,
        settings=settings,
        limiter=limiter,
    )
    if response is None or ownership_report is None:
        return {
            "group_id": profile.group_id,
            "status": "failed",
            "failed_stage": "ownership",
            "source_member_count": len(profile.members),
            "stages": {
                "ownership": {"status": "failed", "attempts": ownership_attempts},
                "definitions": [],
            },
        }

    plans = tuple(response.final_field_plans)
    definition_results = await asyncio.gather(
        *(
            _define_final_field(
                profile=profile,
                plan=plan,
                sibling_plans=plans,
                max_validation_retries=max_validation_retries,
                client=client,
                settings=settings,
                limiter=limiter,
            )
            for plan in plans
        )
    )
    final_fields = [field for field, _ in definition_results if field is not None]
    definition_stages = [stage for _, stage in definition_results]
    failed_definitions = [
        stage["plan_id"]
        for stage in definition_stages
        if stage["status"] == "failed"
    ]
    if failed_definitions:
        return {
            "group_id": profile.group_id,
            "status": "failed",
            "failed_stage": "definitions",
            "failed_plan_ids": failed_definitions,
            "source_member_count": len(profile.members),
            "stages": {
                "ownership": {"status": "succeeded", "attempts": ownership_attempts},
                "definitions": definition_stages,
            },
        }

    return {
        "group_id": profile.group_id,
        "status": "succeeded",
        "source_member_count": len(profile.members),
        "reason": ownership_report["reason"],
        "decision": ownership_report["decision"],
        "ownership_plan": ownership_report["final_field_plans"],
        "final_fields": final_fields,
        "discarded_candidates": ownership_report["discarded_candidates"],
        "input_candidate_ids": ownership_report["input_candidate_ids"],
        "stages": {
            "ownership": {"status": "succeeded", "attempts": ownership_attempts},
            "definitions": definition_stages,
        },
    }


async def refine_candidate_groups(
    *,
    profiles: Sequence[GroupProfile],
    max_members_per_group: int,
    max_validation_retries: int,
    client: AsyncOpenAI,
    settings: ProjectSettings,
    limiter: ModelRequestLimiter,
    emit: LogEmitter,
) -> list[dict[str, Any]]:
    """并发收敛全部关系图分量，并稳定隔离单组异常。"""

    async def refine_one(profile: GroupProfile) -> dict[str, Any]:
        try:
            result = await refine_group(
                profile=profile,
                max_members_per_group=max_members_per_group,
                max_validation_retries=max_validation_retries,
                client=client,
                settings=settings,
                limiter=limiter,
            )
        except Exception as error:
            error_record = _safe_error(error)
            result = {
                "group_id": profile.group_id,
                "status": "failed",
                "failed_stage": "unexpected",
                "source_member_count": len(profile.members),
                **error_record,
            }
        await emit(
            f"[GROUP {profile.group_id}] {result['status']}｜输入={len(profile.members)}｜"
            f"最终字段={len(result.get('final_fields', []))}｜"
            f"淘汰={len(result.get('discarded_candidates', []))}｜"
            f"失败阶段={result.get('failed_stage', '-')}"
        )
        return result

    reports = list(await asyncio.gather(*(refine_one(profile) for profile in profiles)))
    reports.sort(key=lambda item: str(item["group_id"]))
    return reports


async def run_global_semantic_gate(
    *,
    final_fields: Sequence[dict[str, Any]],
    fixed_definitions: Sequence[FieldDefinition],
    max_validation_retries: int,
    client: AsyncOpenAI,
    settings: ProjectSettings,
    limiter: ModelRequestLimiter,
) -> dict[str, Any]:
    """在跨组层面检查固定覆盖、同义重复和边界重叠；冲突不被静默删除。"""

    if not final_fields:
        return {
            "status": "passed",
            "decision_count": 0,
            "conflict_count": 0,
            "decisions": [],
            "attempts": [],
        }
    final_field_refs = {
        f"{item['group_id']}:{item['definition']['field_id']}" for item in final_fields
    }
    final_field_by_ref = {
        f"{item['group_id']}:{item['definition']['field_id']}": item["definition"]
        for item in final_fields
    }
    fixed_field_by_ref = {
        f"fixed:{definition.field_id}": field_definition_record(definition)
        for definition in fixed_definitions
    }

    async def judge_one(item: dict[str, Any]) -> dict[str, Any]:
        current_ref = f"{item['group_id']}:{item['definition']['field_id']}"
        prompt = build_global_semantic_gate_prompt(
            final_fields=final_fields,
            fixed_definitions=fixed_definitions,
            current_field_ref=current_ref,
        )
        attempts: list[dict[str, Any]] = []
        correction: str | None = None
        for attempt in range(max_validation_retries + 1):
            metrics: dict[str, Any] | None = None
            try:
                response, metrics = await _invoke_json(
                    prompt=prompt,
                    correction=correction,
                    schema_model=SingleGlobalSemanticDecision,
                    schema_name="field_discovery_global_semantic_decision",
                    phase_label=f"全局语义门禁 {current_ref}",
                    client=client,
                    settings=settings,
                    limiter=limiter,
                    system_message=GLOBAL_GATE_SYSTEM_MESSAGE,
                    max_completion_tokens=3072,
                )
                decision = validate_single_global_semantic_decision(
                    response=response,
                    current_field_ref=current_ref,
                    final_field_refs=final_field_refs,
                    fixed_definitions=fixed_definitions,
                )
            except Exception as error:
                error_record = _safe_error(error)
                if metrics is not None and "metrics" not in error_record:
                    error_record["metrics"] = metrics
                attempts.append(
                    {"attempt": attempt + 1, "status": "rejected", **error_record}
                )
                correction = (
                    str(error_record["error"])
                    + "\n当前字段由程序固定为 "
                    + current_ref
                    + "；请只输出它的一次判断。"
                )
                continue
            attempts.append(
                {"attempt": attempt + 1, "status": "accepted", "metrics": metrics}
            )
            confirmation_attempts: list[dict[str, Any]] = []
            if decision.status != "accepted":
                target_field = {
                    **fixed_field_by_ref,
                    **final_field_by_ref,
                }.get(str(decision.target_ref))
                if target_field is None:
                    raise AssertionError("已校验的全局门禁目标必须能解析为字段定义。")
                confirmation_prompt = build_global_conflict_confirmation_prompt(
                    current_field=item["definition"],
                    target_field=target_field,
                    proposed_decision=decision,
                )
                confirmation_correction: str | None = None
                confirmation: GlobalConflictConfirmation | None = None
                for confirmation_attempt in range(max_validation_retries + 1):
                    confirmation_metrics: dict[str, Any] | None = None
                    try:
                        confirmation, confirmation_metrics = await _invoke_json(
                            prompt=confirmation_prompt,
                            correction=confirmation_correction,
                            schema_model=GlobalConflictConfirmation,
                            schema_name="field_discovery_global_conflict_confirmation",
                            phase_label=f"全局冲突复核 {current_ref}",
                            client=client,
                            settings=settings,
                            limiter=limiter,
                            system_message=GLOBAL_GATE_SYSTEM_MESSAGE,
                            max_completion_tokens=2048,
                        )
                    except Exception as error:
                        error_record = _safe_error(error)
                        if (
                            confirmation_metrics is not None
                            and "metrics" not in error_record
                        ):
                            error_record["metrics"] = confirmation_metrics
                        confirmation_attempts.append(
                            {
                                "attempt": confirmation_attempt + 1,
                                "status": "rejected",
                                **error_record,
                            }
                        )
                        confirmation_correction = str(error_record["error"])
                        continue
                    confirmation_attempts.append(
                        {
                            "attempt": confirmation_attempt + 1,
                            "status": "accepted",
                            "metrics": confirmation_metrics,
                        }
                    )
                    break
                if confirmation is None:
                    return {
                        "final_field_ref": current_ref,
                        "status": "failed",
                        "attempts": attempts,
                        "confirmation_attempts": confirmation_attempts,
                    }
                if confirmation.status == "false_positive":
                    decision = GlobalSemanticDecision(
                        final_field_ref=current_ref,
                        target_ref=None,
                        reason=(
                            "全局初判冲突经聚焦字段对复核后被否决："
                            + confirmation.reason
                        ),
                        status="accepted",
                    )
            return {
                "final_field_ref": current_ref,
                "status": "succeeded",
                "decision": decision.model_dump(mode="json"),
                "attempts": attempts,
                "confirmation_attempts": confirmation_attempts,
            }
        return {
            "final_field_ref": current_ref,
            "status": "failed",
            "attempts": attempts,
        }

    field_results = list(await asyncio.gather(*(judge_one(item) for item in final_fields)))
    field_results.sort(key=lambda item: item["final_field_ref"])
    decisions = [
        item["decision"] for item in field_results if item["status"] == "succeeded"
    ]
    failed_fields = [
        item["final_field_ref"] for item in field_results if item["status"] == "failed"
    ]
    conflicts = [item for item in decisions if item["status"] != "accepted"]
    return {
        "status": "passed" if not failed_fields and not conflicts else "failed",
        "decision_count": len(decisions),
        "conflict_count": len(conflicts),
        "failed_field_count": len(failed_fields),
        "failed_field_refs": failed_fields,
        "decisions": decisions,
        "field_results": field_results,
        "attempts": [],
    }


def build_refinement_plan(
    *,
    profiles: Sequence[GroupProfile],
    reports: list[dict[str, Any]],
    semantic_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行跨组字段 ID 门禁，并汇总全局语义门禁状态。"""

    try:
        validate_batch_field_ids(reports)
    except ValueError as error:
        for report in reports:
            if report.get("status") == "succeeded":
                report["batch_gate_error"] = str(error)
        batch_gate_status = "failed"
    else:
        batch_gate_status = "passed"

    final_fields = [
        {"group_id": report["group_id"], **field}
        for report in reports
        if report.get("status") == "succeeded"
        for field in report.get("final_fields", [])
    ]
    semantic_gate_report = semantic_gate or {
        "status": "not_run",
        "decision_count": 0,
        "conflict_count": 0,
        "decisions": [],
        "attempts": [],
    }
    return {
        "mode": "relation_graph_two_stage_refinement",
        "source_group_count": len(profiles),
        "source_identity_count": sum(len(profile.members) for profile in profiles),
        "succeeded_group_count": sum(item.get("status") == "succeeded" for item in reports),
        "failed_group_count": sum(item.get("status") == "failed" for item in reports),
        "final_field_count": len(final_fields),
        "discarded_candidate_count": sum(
            len(item.get("discarded_candidates", [])) for item in reports
        ),
        "batch_field_id_gate": batch_gate_status,
        "batch_semantic_gate": semantic_gate_report["status"],
        "semantic_gate": semantic_gate_report,
        "final_fields": final_fields,
    }
