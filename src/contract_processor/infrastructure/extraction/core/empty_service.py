"""发现模式 0 Core 冷启动的确定性空策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.extraction.stage_result import StageResult


@dataclass(frozen=True, slots=True)
class EmptyCoreExtractionService:
    """在显式空目录下返回合法空 Core，且绝不调用模型。"""

    schema_version: str

    async def extract(
        self, context: PdfExtractionContext
    ) -> StageResult[dict[str, Any]]:
        return StageResult(
            payload={"document_id": context.document_id, "fields": {}},
            validation={
                "is_valid": True,
                "mode": "empty_catalog",
                "configured_field_count": 0,
                "core_schema_version": self.schema_version,
            },
            metrics={"model_calls": 0},
        )
