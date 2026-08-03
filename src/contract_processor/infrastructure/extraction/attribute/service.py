"""固定 Attribute 抽取服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contract_processor.infrastructure.extraction.attribute.pipeline import (
    run_attribute_extraction,
)
from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.extraction.stage_result import StageResult


class AttributeExtractionService:
    """把目录驱动的 Attribute 算法暴露为正式服务接口。"""

    def __init__(
        self,
        *,
        core_catalog_path: Path | None = None,
        attribute_catalog_path: Path | None = None,
    ) -> None:
        # 默认 None 保持 production 的配置路径；实验可注入隔离 Discovery 目录。
        self._core_catalog_path = core_catalog_path
        self._attribute_catalog_path = attribute_catalog_path

    async def extract(
        self,
        context: PdfExtractionContext,
        *,
        core_fields: dict[str, Any],
        contract_understanding_bullets: str,
    ) -> StageResult[list[dict[str, Any]]]:
        return await run_attribute_extraction(
            project_root_path=context.project_root,
            document_id=context.document_id,
            shared_images=context.images,
            shared_source_page_count=context.source_page_count,
            shared_client=context.client,
            model_request_limiter=context.model_request_limiter,
            core_fields=core_fields,
            contract_understanding_bullets=contract_understanding_bullets,
            core_catalog_path=self._core_catalog_path,
            attribute_catalog_path=self._attribute_catalog_path,
        )
