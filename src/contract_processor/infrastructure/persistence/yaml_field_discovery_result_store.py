"""字段发现最终定义与频率统计的 YAML 持久化。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from contract_processor.application.schemas.field_discovery import (
    FieldDiscoveryBatchResult,
)
from contract_processor.async_utils import run_blocking


class YamlFieldDiscoveryResultStore:
    """以批次 ID 命名并原子保存待审核字段目录。

    每个字段只在正式 Attribute 定义上增加 ``statistics``。该文件是 discovery
    待审核产物，不会修改或自动晋级到正式 ``attribute.yaml``。
    """

    # 0.2 增加第一阶段硬失败与 Attribute 局部失败诊断，便于审核时区分两类失败。
    SCHEMA_VERSION = "0.2"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    async def save(self, result: FieldDiscoveryBatchResult) -> Path:
        return await run_blocking(self._save_sync, result)

    def _save_sync(self, result: FieldDiscoveryBatchResult) -> Path:
        payload = self._build_payload(result)
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / f"{result.batch_id}.yaml"
        temporary = self._root / f".{result.batch_id}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(
                    payload,
                    stream,
                    allow_unicode=True,
                    sort_keys=False,
                    width=120,
                )
                stream.flush()
                os.fsync(stream.fileno())
            # 同目录原子替换，避免异常退出后留下可见的半份 YAML。
            temporary.replace(target)
            directory_fd = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @classmethod
    def _build_payload(cls, result: FieldDiscoveryBatchResult) -> dict[str, Any]:
        found_source_names: dict[str, list[str]] = {}
        failed_source_names: dict[str, list[str]] = {}
        for observation in result.stage_two.observations:
            if observation.task_status == "failed":
                failed_source_names.setdefault(observation.candidate_ref, []).append(
                    observation.source_name
                )
            elif observation.extraction is not None and observation.extraction.get(
                "status"
            ) == "found":
                found_source_names.setdefault(observation.candidate_ref, []).append(
                    observation.source_name
                )

        statistics_by_ref: dict[str, dict[str, Any]] = {}
        for statistics in result.stage_two.statistics:
            if statistics.candidate_ref in statistics_by_ref:
                raise ValueError(
                    f"第二阶段包含重复候选统计：{statistics.candidate_ref}。"
                )
            statistics_by_ref[statistics.candidate_ref] = statistics.model_dump(
                mode="json"
            )

        fields: list[dict[str, Any]] = []
        frozen_refs: set[str] = set()
        for candidate in result.stage_one.frozen_candidates:
            candidate_ref = candidate.get("candidate_ref")
            definition = candidate.get("definition")
            if not isinstance(candidate_ref, str) or not candidate_ref:
                raise ValueError("冻结候选缺少 candidate_ref，无法关联统计。")
            if candidate_ref in frozen_refs:
                raise ValueError(f"第一阶段包含重复冻结候选：{candidate_ref}。")
            if not isinstance(definition, dict):
                raise ValueError(f"冻结候选 {candidate_ref} 缺少字段定义。")
            statistics = statistics_by_ref.get(candidate_ref)
            if statistics is None:
                raise ValueError(f"冻结候选 {candidate_ref} 缺少第二阶段统计。")
            if (
                statistics["field_id"] != definition.get("field_id")
                or statistics["field_name"] != definition.get("name")
            ):
                raise ValueError(f"冻结候选 {candidate_ref} 的字段身份与统计不一致。")
            # DTO 使用 document_id 保证去重和计算稳定；人工审核 YAML 改用源文件名溯源。
            statistics.pop("found_document_ids")
            statistics.pop("failed_document_ids")
            frozen_refs.add(candidate_ref)
            # 统计对象同时携带治理来源；字段定义本体保持 Attribute 目录结构。
            fields.append(
                {
                    **definition,
                    "statistics": {
                        "candidate_ref": statistics.pop("candidate_ref"),
                        "group_id": candidate.get("group_id"),
                        "source_candidate_ids": candidate.get(
                            "source_candidate_ids", []
                        ),
                        **statistics,
                        "found_source_names": found_source_names.get(
                            candidate_ref, []
                        ),
                        "failed_source_names": failed_source_names.get(
                            candidate_ref, []
                        ),
                    },
                }
            )

        orphan_refs = set(statistics_by_ref) - frozen_refs
        if orphan_refs:
            raise ValueError(
                "第二阶段包含不属于冻结字段的统计："
                + "、".join(sorted(orphan_refs))
                + "。"
            )

        return {
            "schema_version": cls.SCHEMA_VERSION,
            "field_set": "discovery",
            "status": "draft",
            "batch": {
                "batch_id": result.batch_id,
                "started_at": result.started_at.isoformat(),
                "completed_at": result.completed_at.isoformat(),
                "document_count": result.stage_two.document_count,
                "stage_one_status": result.stage_one.status,
                "stage_two_status": result.stage_two.status,
                "succeeded_document_count": result.stage_one.succeeded_document_count,
                "failed_document_count": result.stage_one.failed_document_count,
                "partial_attribute_document_count": (
                    result.stage_one.partial_attribute_document_count
                ),
                "failed_documents": result.stage_one.failed_documents,
                "partial_attribute_documents": (
                    result.stage_one.partial_attribute_documents
                ),
                "processing": result.processing.model_dump(mode="json"),
            },
            "fields": fields,
        }
