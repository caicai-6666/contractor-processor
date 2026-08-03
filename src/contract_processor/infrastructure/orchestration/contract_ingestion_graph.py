"""专家终审入库专用 LangGraph 拓扑。"""

from collections.abc import Awaitable, Callable
from typing import Any

from contract_processor.application.workflows.contract_ingestion import (
    ContractIngestionState,
)


IngestionNode = Callable[
    [ContractIngestionState], Awaitable[dict[str, Any]]
]


class ContractIngestionGraphFactory:
    """构建与合同提取主图物理解耦的四节点入库图。"""

    def build_contract_ingestion(
        self,
        *,
        prepare_ingestion: IngestionNode,
        embed_text_fields: IngestionNode,
        embed_pdf_visual: IngestionNode,
        persist_contract: IngestionNode,
    ) -> Any:
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(ContractIngestionState)
        graph.add_node("prepare_ingestion", prepare_ingestion)
        graph.add_node("embed_text_fields", embed_text_fields)
        graph.add_node("embed_pdf_visual", embed_pdf_visual)
        graph.add_node("persist_contract", persist_contract)
        graph.add_edge(START, "prepare_ingestion")
        graph.add_edge("prepare_ingestion", "embed_text_fields")
        graph.add_edge("prepare_ingestion", "embed_pdf_visual")
        # LangGraph 只在两个并行向量节点都结束后进入统一提交边界。
        graph.add_edge(
            ["embed_text_fields", "embed_pdf_visual"],
            "persist_contract",
        )
        graph.add_edge("persist_contract", END)
        return graph.compile()
