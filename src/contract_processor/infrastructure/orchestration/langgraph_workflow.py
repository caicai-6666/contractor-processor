"""LangGraph 图组装的预留位置。"""

from collections.abc import Callable
from typing import Any

from contract_processor.application.workflows.state import ContractDiscoveryState


class LangGraphWorkflowFactory:
    """仅负责图拓扑，具体业务节点由 application 层提供。"""

    def build(self, extract_core: Callable[[ContractDiscoveryState], dict[str, Any]]) -> Any:
        # 延迟导入使本地领域测试不依赖 LangGraph 运行时。
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(ContractDiscoveryState)
        graph.add_node("extract_core", extract_core)
        graph.add_edge(START, "extract_core")
        graph.add_edge("extract_core", END)
        return graph.compile()
