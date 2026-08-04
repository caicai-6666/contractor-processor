"""字段发现批次用例：第一阶段发现，第二阶段逐字段回扫与统计。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from contract_processor.application.ports.contracts import FieldDiscoveryResultStore
from contract_processor.application.schemas.field_discovery import (
    CandidateFieldObservation,
    FieldDiscoveryBatchProcessingMetadata,
    FieldDiscoveryBatchResult,
    FieldDiscoveryResult,
)
from contract_processor.async_utils import run_blocking


class AsyncBatchDiscoveryGraph(Protocol):
    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        """执行 discovery 批次父图或第二阶段子图。"""


class BatchDiscoveryGraphFactory(Protocol):
    def build_field_discovery_batch(self, **nodes: Any) -> AsyncBatchDiscoveryGraph:
        """根据阶段节点构造父图。"""

    def build_field_discovery_stage_two(self, **nodes: Any) -> AsyncBatchDiscoveryGraph:
        """构造单字段并发提取与频率统计两节点子图。"""


def _frequency(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


class DiscoverFieldsFromBatch:
    """在固定合同集上完成两阶段 discovery。

    第一阶段按合同顺序运行，避免多份全页视觉上下文同时占满本地模型。第二阶段将
    ``不同合同 × 冻结候选`` 展开为独立任务，由 LangGraph 动态并发；模型实际并发仍由
    基础设施共享限流器控制。
    """

    def __init__(
        self,
        *,
        project_root: Path,
        discover_one: Callable[[Path], Awaitable[FieldDiscoveryResult]],
        consolidate_candidates: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        extract_candidate_field: Callable[
            [dict[str, Any]], Awaitable[dict[str, Any]]
        ],
        identify_document: Callable[[Path], Awaitable[str]],
        close_stage_one: Callable[[], Awaitable[None]] | None = None,
        close_candidate_extractor: Callable[[], Awaitable[None]],
        graph_factory: BatchDiscoveryGraphFactory,
        processing_metadata: FieldDiscoveryBatchProcessingMetadata,
        result_store: FieldDiscoveryResultStore | None = None,
        emit_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._project_root = project_root
        self._discover_one = discover_one
        self._consolidate_candidates = consolidate_candidates
        self._extract_candidate_field = extract_candidate_field
        self._identify_document = identify_document
        self._close_stage_one = close_stage_one
        self._close_candidate_extractor = close_candidate_extractor
        self._graph_factory = graph_factory
        self._processing_metadata = processing_metadata
        self._result_store = result_store
        self._emit_progress = emit_progress

    async def _emit(self, message: str) -> None:
        if self._emit_progress is not None:
            await self._emit_progress(message)

    async def execute(self, pdf_paths: Sequence[Path]) -> FieldDiscoveryBatchResult:
        """执行整批发现，并在路径、身份或图执行失败时统一释放共享资源。"""

        started_at = datetime.now(UTC)
        batch_id = "discovery-" + started_at.strftime("%Y%m%dT%H%M%S%fZ")
        try:
            result = await self._execute(
                pdf_paths,
                batch_id=batch_id,
                started_at=started_at,
            )
            if self._result_store is not None:
                result_path = await self._result_store.save(result)
                await self._emit(f"[DISCOVERY RESULT] YAML 已写入：{result_path}")
            return result
        finally:
            closers = [self._close_candidate_extractor()]
            if self._close_stage_one is not None:
                closers.append(self._close_stage_one())
            await asyncio.gather(*closers)

    async def _execute(
        self,
        pdf_paths: Sequence[Path],
        *,
        batch_id: str,
        started_at: datetime,
    ) -> FieldDiscoveryBatchResult:
        resolved_paths: list[Path] = []
        for path in pdf_paths:
            candidate = path if path.is_absolute() else self._project_root / path
            resolved = await run_blocking(candidate.resolve)
            if not await run_blocking(resolved.is_file):
                raise FileNotFoundError(f"找不到待发现字段的 PDF：{resolved}")
            resolved_paths.append(resolved)
        if not resolved_paths:
            raise ValueError("字段发现批次至少需要一份 PDF。")

        document_ids = await asyncio.gather(
            *(self._identify_document(path) for path in resolved_paths)
        )
        # 字节相同的副本具有同一 document_id，频率统计只计为一份不同合同。
        unique_documents: list[dict[str, Any]] = []
        seen_document_ids: set[str] = set()
        for path, document_id in zip(resolved_paths, document_ids, strict=True):
            if document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            unique_documents.append(
                {
                    "contract_path": path,
                    "document_id": document_id,
                    "source_name": path.name,
                }
            )

        async def run_stage_one(state: dict[str, Any]) -> dict[str, Any]:
            documents: list[FieldDiscoveryResult] = []
            failed_documents: list[dict[str, str]] = []
            raw_candidates: list[dict[str, Any]] = []
            for document_index, path in enumerate(state["contract_paths"], start=1):
                await self._emit(
                    f"[STAGE1 {document_index}/{len(state['contract_paths'])}] "
                    f"{path.name} 开始"
                )
                try:
                    document = await self._discover_one(path)
                except Exception as error:
                    # 单份合同固定字段失败不应吞掉其他合同已准入的候选，也不能阻断回扫。
                    failed_documents.append(
                        {
                            "source_name": path.name,
                            "error_type": type(error).__name__,
                            "error": str(error)[:1200],
                        }
                    )
                    await self._emit(
                        f"[STAGE1 {document_index}/{len(state['contract_paths'])}] "
                        f"{path.name} 失败：{type(error).__name__}: {str(error)[:300]}"
                    )
                    continue
                documents.append(document)
                for index, definition in enumerate(document.candidates, start=1):
                    raw_candidates.append(
                        {
                            "candidate_ref": f"{document.document_id}:{index}",
                            "document_id": document.document_id,
                            "source_name": document.source_name,
                            "definition": definition,
                        }
                    )
                await self._emit(
                    f"[STAGE1 {document_index}/{len(state['contract_paths'])}] "
                    f"{path.name} 完成：模型候选="
                    f"{document.discovery_metrics.get('model_candidate_count', 0)}，"
                    f"准入={document.discovery_metrics.get('accepted_candidate_count', 0)}，"
                    f"拒绝={document.discovery_metrics.get('rejected_candidate_count', 0)}"
                )
            if self._consolidate_candidates is None:
                # 仅供注入假服务的应用层测试使用；正式容器始终提供完整批次收敛器。
                consolidation = {
                    "status": "completed",
                    "candidate_identity_count": len(raw_candidates),
                    "source_group_count": len(raw_candidates),
                    "succeeded_group_count": len(raw_candidates),
                    "partially_succeeded_group_count": 0,
                    "failed_group_count": 0,
                    "final_field_count": len(raw_candidates),
                    "discarded_candidate_count": 0,
                    "batch_field_id_gate": "passed",
                    "batch_semantic_gate": "passed",
                    "candidate_pool": [],
                    "relation_graph": {"edges": [], "components": []},
                    "group_refinements": [],
                    "global_semantic_gate": {
                        "status": "not_run_test_fallback"
                    },
                    "frozen_fields": raw_candidates,
                }
            else:
                await self._emit("[STAGE1 CONSOLIDATE] 候选池冻结，开始关系图分组收敛")
                consolidation = await self._consolidate_candidates()
                await self._emit(
                    "[STAGE1 CONSOLIDATE] 完成："
                    f"身份={consolidation['candidate_identity_count']}，"
                    f"分组={consolidation['source_group_count']}，"
                    f"冻结字段={len(consolidation['frozen_fields'])}，"
                    f"部分成功分组={consolidation['partially_succeeded_group_count']}，"
                    f"失败分组={consolidation['failed_group_count']}，"
                    f"全局门禁={consolidation['batch_semantic_gate']}"
                )
            frozen_candidates = consolidation["frozen_fields"]
            stage_has_failures = bool(failed_documents) or (
                consolidation["status"] == "completed_with_failures"
            )
            return {
                "stage_one": {
                    "status": "completed_with_failures" if stage_has_failures else "completed",
                    "document_count": len(state["contract_paths"]),
                    "succeeded_document_count": len(documents),
                    "failed_document_count": len(failed_documents),
                    "candidate_count": len(frozen_candidates),
                    "raw_candidate_count": len(raw_candidates),
                    "candidate_identity_count": consolidation[
                        "candidate_identity_count"
                    ],
                    "source_group_count": consolidation["source_group_count"],
                    "succeeded_group_count": consolidation[
                        "succeeded_group_count"
                    ],
                    "partially_succeeded_group_count": consolidation[
                        "partially_succeeded_group_count"
                    ],
                    "failed_group_count": consolidation["failed_group_count"],
                    "final_field_count": consolidation["final_field_count"],
                    "discarded_candidate_count": consolidation[
                        "discarded_candidate_count"
                    ],
                    "batch_field_id_gate": consolidation["batch_field_id_gate"],
                    "batch_semantic_gate": consolidation["batch_semantic_gate"],
                    "documents": [
                        item.model_dump(mode="json") for item in documents
                    ],
                    "failed_documents": failed_documents,
                    "candidate_pool": consolidation["candidate_pool"],
                    "relation_graph": consolidation["relation_graph"],
                    "group_refinements": consolidation["group_refinements"],
                    "global_semantic_gate": consolidation[
                        "global_semantic_gate"
                    ],
                    "frozen_candidates": frozen_candidates,
                }
            }

        async def run_stage_two(state: dict[str, Any]) -> dict[str, Any]:
            candidates = state["stage_one"]["frozen_candidates"]
            batch_documents = state["batch_documents"]
            tasks: list[dict[str, Any]] = []
            for candidate_index, candidate in enumerate(candidates):
                definition = candidate["definition"]
                for document_index, document in enumerate(batch_documents):
                    tasks.append(
                        {
                            "task_ref": (
                                f"{candidate['candidate_ref']}@{document['document_id']}"
                            ),
                            "candidate_ref": candidate["candidate_ref"],
                            "candidate_index": candidate_index,
                            "field_id": definition["field_id"],
                            "field_name": definition["name"],
                            "definition": definition,
                            "document_index": document_index,
                            **document,
                        }
                    )

            async def extract_candidate_field(
                task_state: dict[str, Any],
            ) -> dict[str, Any]:
                task = task_state["extraction_task"]
                base = {
                    key: task[key]
                    for key in (
                        "task_ref",
                        "candidate_ref",
                        "field_id",
                        "field_name",
                        "document_id",
                        "source_name",
                    )
                }
                try:
                    extracted = await self._extract_candidate_field(task)
                    observation = CandidateFieldObservation.model_validate(
                        {
                            **base,
                            "task_status": "succeeded",
                            "extraction": extracted["extraction"],
                            "attempt_count": extracted.get("attempt_count", 1),
                            "metrics": extracted.get("metrics", {}),
                        }
                    )
                except Exception as error:
                    # 渲染、网关、Schema 和业务校验失败都只归属于当前字段—合同任务。
                    observation = CandidateFieldObservation.model_validate(
                        {
                            **base,
                            "task_status": "failed",
                            "attempt_count": getattr(error, "attempt_count", 0),
                            "error_type": type(error).__name__,
                            "error": str(error)[:1200] or repr(error)[:1200],
                            "metrics": getattr(error, "metrics", {}),
                        }
                    )
                return {"observations": [observation.model_dump(mode="json")]}

            async def calculate_candidate_statistics(
                statistics_state: dict[str, Any],
            ) -> dict[str, Any]:
                observations = statistics_state.get("observations", [])
                order = {task["task_ref"]: index for index, task in enumerate(tasks)}
                observations.sort(key=lambda item: order[item["task_ref"]])
                statistics: list[dict[str, Any]] = []
                statuses = (
                    "found",
                    "not_found",
                    "ambiguous",
                    "conflicting",
                    "not_applicable",
                )
                for candidate in candidates:
                    definition = candidate["definition"]
                    candidate_observations = [
                        item
                        for item in observations
                        if item["candidate_ref"] == candidate["candidate_ref"]
                    ]
                    status_counts = {
                        status: sum(
                            item["task_status"] == "succeeded"
                            and item["extraction"]["status"] == status
                            for item in candidate_observations
                        )
                        for status in statuses
                    }
                    failed = [
                        item
                        for item in candidate_observations
                        if item["task_status"] == "failed"
                    ]
                    scanned = sum(status_counts.values())
                    found = status_counts["found"]
                    statistics.append(
                        {
                            "candidate_ref": candidate["candidate_ref"],
                            "field_id": definition["field_id"],
                            "field_name": definition["name"],
                            "document_count": len(batch_documents),
                            "scanned_document_count": scanned,
                            "found_document_count": found,
                            "not_found_document_count": status_counts["not_found"],
                            "ambiguous_document_count": status_counts["ambiguous"],
                            "conflicting_document_count": status_counts["conflicting"],
                            "not_applicable_document_count": status_counts[
                                "not_applicable"
                            ],
                            "failed_document_count": len(failed),
                            "frequency": _frequency(found, scanned),
                            "conservative_frequency": _frequency(
                                found, len(batch_documents)
                            ),
                            "found_document_ids": [
                                item["document_id"]
                                for item in candidate_observations
                                if item["task_status"] == "succeeded"
                                and item["extraction"]["status"] == "found"
                            ],
                            "failed_document_ids": [
                                item["document_id"] for item in failed
                            ],
                        }
                    )
                failed_count = sum(
                    item["task_status"] == "failed" for item in observations
                )
                return {
                    "stage_two": {
                        "status": (
                            "completed_with_failures"
                            if failed_count
                            else "completed"
                        ),
                        "received_candidate_count": len(candidates),
                        "document_count": len(batch_documents),
                        "task_count": len(tasks),
                        "succeeded_task_count": len(observations) - failed_count,
                        "failed_task_count": failed_count,
                        "observations": observations,
                        "statistics": statistics,
                    }
                }

            stage_two_graph = self._graph_factory.build_field_discovery_stage_two(
                extract_candidate_field=extract_candidate_field,
                calculate_candidate_statistics=calculate_candidate_statistics,
            )
            await self._emit(
                f"[STAGE2] 开始单合同单字段并发回扫：字段={len(candidates)}，"
                f"合同={len(batch_documents)}，任务={len(tasks)}"
            )
            stage_two_state = await stage_two_graph.ainvoke(
                {"extraction_tasks": tasks, "observations": []}
            )
            stage_two_result = stage_two_state["stage_two"]
            await self._emit(
                f"[STAGE2] 完成：成功={stage_two_result['succeeded_task_count']}，"
                f"失败={stage_two_result['failed_task_count']}"
            )
            return {"stage_two": stage_two_result}

        graph = self._graph_factory.build_field_discovery_batch(
            run_stage_one=run_stage_one,
            run_stage_two=run_stage_two,
        )
        state = await graph.ainvoke(
            {
                "contract_paths": [
                    item["contract_path"] for item in unique_documents
                ],
                "batch_documents": unique_documents,
            }
        )
        return FieldDiscoveryBatchResult.model_validate(
            {
                "mode": "discovery",
                "batch_id": batch_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC),
                "processing": self._processing_metadata.model_dump(mode="json"),
                "stage_one": state["stage_one"],
                "stage_two": state["stage_two"],
            }
        )
