"""Core 抽取服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.extraction.core.pipeline import (
    run_core_extraction,
)
from contract_processor.infrastructure.extraction.stage_result import StageResult


class CoreExtractionService:
    """把 Core 算法暴露为不依赖 CLI 的正式服务接口。"""

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
        self, context: PdfExtractionContext
    ) -> StageResult[dict[str, Any]]:
        return await run_core_extraction(
            pdf_path=context.pdf_path,
            document_id=context.document_id,
            project_root_path=context.project_root,
            shared_images=context.images,
            shared_source_page_count=context.source_page_count,
            shared_client=context.client,
            model_request_limiter=context.model_request_limiter,
            core_catalog_path=self._core_catalog_path,
            attribute_catalog_path=self._attribute_catalog_path,
        )
