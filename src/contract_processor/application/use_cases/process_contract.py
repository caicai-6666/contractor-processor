"""处理单份合同并形成统一终审候选。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from contract_processor.application.schemas.contract_processing import ContractProcessingResult
from contract_processor.application.workflows.contract_processing import (
    ContractExtractionPipelines,
    ContractProcessingNodes,
)
from contract_processor.async_utils import run_blocking


class AsyncContractGraph(Protocol):
    """应用层使用的最小异步图接口。"""

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        """异步执行图并返回最终状态。"""


class ContractGraphFactory(Protocol):
    """应用层只依赖可调用图协议，不感知 LangGraph 类型。"""

    def build_contract_processing(self, **nodes: Any) -> AsyncContractGraph:
        """根据注入节点构建可执行图。"""


class ProcessContract:
    """统一工作流应用用例；负责事务边界，不包含任何字段匹配数组。"""

    def __init__(
        self,
        *,
        project_root: Path,
        pipelines: ContractExtractionPipelines,
        graph_factory: ContractGraphFactory,
    ) -> None:
        self._project_root = project_root
        self._pipelines = pipelines
        self._graph_factory = graph_factory

    async def execute(self, pdf_path: Path) -> ContractProcessingResult:
        """异步执行四类子图并返回无落盘副作用的聚合候选。"""

        candidate_pdf = (
            pdf_path if pdf_path.is_absolute() else self._project_root / pdf_path
        )
        resolved_pdf = await run_blocking(candidate_pdf.resolve)
        if not await run_blocking(resolved_pdf.is_file):
            raise FileNotFoundError(f"找不到待处理 PDF：{resolved_pdf}")

        nodes = ContractProcessingNodes(self._pipelines)
        graph = self._graph_factory.build_contract_processing(
            prepare=nodes.prepare,
            extract_core=nodes.extract_core,
            extract_attributes=(
                None
                if self._pipelines.attribute_catalog_mode == "empty_catalog"
                else nodes.extract_attributes
            ),
            extract_clauses=nodes.extract_clauses,
            extract_abstract=nodes.extract_abstract,
            finalize=nodes.finalize,
        )
        try:
            state = await graph.ainvoke(
                {
                    "contract_path": resolved_pdf,
                    "attribute_result": [],
                    "errors": [],
                }
            )
        finally:
            await self._pipelines.close()

        result = ContractProcessingResult.model_validate(
            {
                "document_id": state["document_id"],
                "source_name": state["source_name"],
                "core": state["core_result"],
                "attribute": state["attribute_result"],
                "clause": state["clause_result"],
                "abstract": state["abstract_result"],
                "processing": state["processing_metadata"],
            }
        )
        return result
