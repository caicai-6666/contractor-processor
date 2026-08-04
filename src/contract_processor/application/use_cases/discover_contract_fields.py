"""处理单份合同并形成字段发现候选。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from contract_processor.application.schemas.field_discovery import FieldDiscoveryResult
from contract_processor.application.workflows.field_discovery import (
    FieldDiscoveryNodes,
    FieldDiscoveryPipelines,
)
from contract_processor.async_utils import run_blocking


class AsyncDiscoveryGraph(Protocol):
    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        """异步执行发现图并返回最终状态。"""


class DiscoveryGraphFactory(Protocol):
    def build_field_discovery(self, **nodes: Any) -> AsyncDiscoveryGraph:
        """根据注入节点构建不含 Clause/Abstract 的发现图。"""


class DiscoverContractFields:
    """字段发现用例；结果协议与正式合同提取完全隔离。"""

    def __init__(
        self,
        *,
        project_root: Path,
        pipelines: FieldDiscoveryPipelines,
        graph_factory: DiscoveryGraphFactory,
        close_discovery_service: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._project_root = project_root
        self._pipelines = pipelines
        self._graph_factory = graph_factory
        self._close_discovery_service = close_discovery_service

    async def execute(self, pdf_path: Path) -> FieldDiscoveryResult:
        """执行单合同发现，并在任意入口失败时释放所拥有的全部资源。"""

        try:
            return await self._execute(pdf_path)
        finally:
            # 单合同构建器拥有 discovery 服务时，必须连同其 Embedding 客户端一起关闭；
            # 批次构建器则把共享服务保留到整批收敛完成后再统一关闭。
            try:
                await self._pipelines.close()
            finally:
                if self._close_discovery_service is not None:
                    await self._close_discovery_service()

    async def _execute(self, pdf_path: Path) -> FieldDiscoveryResult:
        candidate_pdf = (
            pdf_path if pdf_path.is_absolute() else self._project_root / pdf_path
        )
        resolved_pdf = await run_blocking(candidate_pdf.resolve)
        if not await run_blocking(resolved_pdf.is_file):
            raise FileNotFoundError(f"找不到待发现字段的 PDF：{resolved_pdf}")

        nodes = FieldDiscoveryNodes(self._pipelines)
        graph = self._graph_factory.build_field_discovery(
            prepare=nodes.prepare,
            extract_core=nodes.extract_core,
            extract_attributes=nodes.extract_attributes,
            discover_fields=nodes.discover_fields,
            finalize=nodes.finalize,
        )
        state = await graph.ainvoke(
            {"contract_path": resolved_pdf, "errors": []}
        )

        return FieldDiscoveryResult.model_validate(
            {
                "mode": "discovery",
                "document_id": state["document_id"],
                "source_name": state["source_name"],
                "core": state["core_result"],
                "candidates": state["field_candidates"],
                "discovery_metrics": state["discovery_metrics"],
                "processing": state["processing_metadata"],
            }
        )
