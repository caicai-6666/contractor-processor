"""Clause 抽取服务。"""

from __future__ import annotations

from typing import Any

from contract_processor.infrastructure.extraction.clause.pipeline import (
    run_clause_extraction,
)
from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.extraction.stage_result import StageResult


class ClauseExtractionService:
    """把 Clause 算法暴露为不依赖 CLI 的正式服务接口。"""

    async def extract(
        self, context: PdfExtractionContext
    ) -> StageResult[dict[str, Any]]:
        return await run_clause_extraction(
            pdf_path=context.pdf_path,
            document_id=context.document_id,
            project_root_path=context.project_root,
            shared_images=context.images,
            shared_source_page_count=context.source_page_count,
            shared_client=context.client,
            model_request_limiter=context.model_request_limiter,
        )
