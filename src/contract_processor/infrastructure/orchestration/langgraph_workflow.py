"""LangGraph 图组装的预留位置。"""

from collections.abc import Awaitable, Callable
from typing import Any

from contract_processor.application.workflows.state import (
    ContractProcessingState,
    FieldDiscoveryState,
)


class LangGraphWorkflowFactory:
    """仅负责图拓扑，具体业务节点由 application 层提供。"""

    def build_contract_processing(
        self,
        *,
        prepare: Callable[[ContractProcessingState], Awaitable[dict[str, Any]]],
        extract_core: Callable[[ContractProcessingState], Awaitable[dict[str, Any]]],
        extract_attributes: (
            Callable[[ContractProcessingState], Awaitable[dict[str, Any]]] | None
        ),
        extract_clauses: Callable[[ContractProcessingState], Awaitable[dict[str, Any]]],
        extract_abstract: Callable[
            [ContractProcessingState], Awaitable[dict[str, Any]]
        ],
        finalize: Callable[[ContractProcessingState], Awaitable[dict[str, Any]]],
    ) -> Any:
        """组装统一合同图；只表达真实的业务数据依赖。

        prepare 完成后，Clause 与 Abstract 可立即读取共享 PDF 并行执行；Attribute 只能在
        Core 结果可用后执行。三条线路共享同一 MLLM 请求门禁，故业务图并行不会放大为
        无界模型并发。finalize 显式等待所有已注册的生产线路完成。
        """

        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(ContractProcessingState)
        graph.add_node("prepare", prepare)
        # 这里直接注册节点而不是包一层单节点子图：子图会回写完整输入状态，三个并行子图
        # 将因此同时写入 contract_path 等只允许单写的状态键。
        graph.add_node("extract_core", extract_core)
        if extract_attributes is not None:
            graph.add_node("extract_attributes", extract_attributes)
        graph.add_node("extract_clauses", extract_clauses)
        graph.add_node("extract_abstract", extract_abstract)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "extract_core")
        graph.add_edge("prepare", "extract_clauses")
        graph.add_edge("prepare", "extract_abstract")
        if extract_attributes is None:
            completed_branches = [
                "extract_core",
                "extract_clauses",
                "extract_abstract",
            ]
        else:
            graph.add_edge("extract_core", "extract_attributes")
            completed_branches = [
                "extract_attributes",
                "extract_clauses",
                "extract_abstract",
            ]
        graph.add_edge(completed_branches, "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    def build_field_discovery(
        self,
        *,
        prepare: Callable[[FieldDiscoveryState], Awaitable[dict[str, Any]]],
        extract_core: Callable[[FieldDiscoveryState], Awaitable[dict[str, Any]]],
        discover_fields: Callable[[FieldDiscoveryState], Awaitable[dict[str, Any]]],
        finalize: Callable[[FieldDiscoveryState], Awaitable[dict[str, Any]]],
    ) -> Any:
        """构建发现专用图，拓扑上不注册 Clause 和 Abstract。"""

        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(FieldDiscoveryState)
        graph.add_node("prepare", prepare)
        graph.add_node("extract_core", extract_core)
        graph.add_node("discover_fields", discover_fields)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "extract_core")
        graph.add_edge("extract_core", "discover_fields")
        graph.add_edge("discover_fields", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()
