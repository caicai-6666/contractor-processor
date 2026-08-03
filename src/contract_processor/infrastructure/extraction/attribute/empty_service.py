"""Attribute 空目录阶段的确定性实现。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from contract_processor.domain.identifiers import validate_document_id
from contract_processor.async_utils import run_blocking
from contract_processor.infrastructure.extraction.stage_result import StageResult


@dataclass(frozen=True, slots=True)
class EmptyAttributeExtractionService:
    """在 Attribute 规范仍为空时生成可审计的空结果。

    该实现是显式占位策略而非静默降级：字段目录一旦出现字段或状态不再为
    ``empty``，服务立即拒绝运行，防止新需求被错误地吞成空数组。
    """

    catalog_path: Path

    async def extract(self, document_id: str) -> StageResult[list[dict[str, Any]]]:
        validate_document_id(document_id)
        catalog = await run_blocking(self._load_catalog)
        fields = catalog.get("fields")
        if catalog.get("status") != "empty" or fields != []:
            raise RuntimeError(
                "Attribute 字段目录已非空，但当前节点仍使用空实现；"
                "请先接入语义发现与归并服务，禁止静默返回空数组。"
            )

        result: list[dict[str, Any]] = []
        return StageResult(
            payload=result,
            validation={
                "is_valid": True,
                "mode": "empty_catalog",
                "document_id": document_id,
                "attribute_schema_version": str(catalog["schema_version"]),
                "candidate_count": 0,
            },
        )

    def _load_catalog(self) -> dict[str, Any]:
        try:
            payload = yaml.safe_load(self.catalog_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise RuntimeError(f"Attribute 字段目录不可读：{self.catalog_path}") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Attribute 字段目录根节点必须是对象。")
        if payload.get("field_set") != "attribute" or "schema_version" not in payload:
            raise RuntimeError("Attribute 字段目录缺少 field_set 或 schema_version。")
        return payload
