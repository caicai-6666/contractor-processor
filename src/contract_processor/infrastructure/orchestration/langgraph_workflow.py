"""LangGraph 图组装的预留位置。"""

from collections.abc import Awaitable, Callable
from typing import Any

from contract_processor.application.workflows.state import (
    ContractProcessingState,
    FieldDiscoveryBatchState,
    FieldDiscoveryStageTwoState,
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
        extract_attributes: Callable[
            [FieldDiscoveryState], Awaitable[dict[str, Any]]
        ],
        discover_fields: Callable[[FieldDiscoveryState], Awaitable[dict[str, Any]]],
        finalize: Callable[[FieldDiscoveryState], Awaitable[dict[str, Any]]],
    ) -> Any:
        """构建发现专用图，拓扑上不注册 Clause 和 Abstract。"""

        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(FieldDiscoveryState)
        graph.add_node("prepare", prepare)
        graph.add_node("extract_core", extract_core)
        graph.add_node("extract_attributes", extract_attributes)
        graph.add_node("discover_fields", discover_fields)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "extract_core")
        graph.add_edge("extract_core", "extract_attributes")
        graph.add_edge("extract_attributes", "discover_fields")
        graph.add_edge("discover_fields", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    def build_field_discovery_batch(
        self,
        *,
        run_stage_one: Callable[
            [FieldDiscoveryBatchState], Awaitable[dict[str, Any]]
        ],
        run_stage_two: Callable[
            [FieldDiscoveryBatchState], Awaitable[dict[str, Any]]
        ],
    ) -> Any:
        """组装 discovery 的两阶段父图。

        第一阶段和第二阶段在拓扑上顺序相连；第二阶段只能读取第一阶段冻结产物，不能回写
        候选身份，也不能在未实现时生成虚假的统计数字。
        """

        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(FieldDiscoveryBatchState)
        graph.add_node("stage_one_discovery", run_stage_one)
        graph.add_node("stage_two_statistics", run_stage_two)
        graph.add_edge(START, "stage_one_discovery")
        graph.add_edge("stage_one_discovery", "stage_two_statistics")
        graph.add_edge("stage_two_statistics", END)
        return graph.compile()

    def build_field_discovery_stage_two(
        self,
        *,
        extract_candidate_field: Callable[
            [FieldDiscoveryStageTwoState], Awaitable[dict[str, Any]]
        ],
        calculate_candidate_statistics: Callable[
            [FieldDiscoveryStageTwoState], Awaitable[dict[str, Any]]
        ],
    ) -> Any:
        """构建单字段动态扇出和频率统计两节点子图。"""

        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Send

        def dispatch_tasks(state: FieldDiscoveryStageTwoState) -> Any:
            tasks = state.get("extraction_tasks", [])
            if not tasks:
                return "calculate_candidate_statistics"
            # 每个 Send 只携带一份合同与一个字段，禁止退化为多字段批量提取。
            return [
                Send("extract_candidate_field", {"extraction_task": task})
                for task in tasks
            ]

        graph = StateGraph(FieldDiscoveryStageTwoState)
        graph.add_node("extract_candidate_field", extract_candidate_field)
        graph.add_node(
            "calculate_candidate_statistics", calculate_candidate_statistics
        )
        graph.add_conditional_edges(START, dispatch_tasks)
        graph.add_edge("extract_candidate_field", "calculate_candidate_statistics")
        graph.add_edge("calculate_candidate_statistics", END)
        return graph.compile()
