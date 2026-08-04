"""两种工作流各自使用的框架无关状态协议。"""

from pathlib import Path
import operator
from typing import Annotated, Any, TypedDict


class PreparedContractState(TypedDict, total=False):
    """两种模式共享的合同准备状态。"""

    contract_path: Path
    document_id: str
    source_name: str
    source_page_count: int
    core_result: dict[str, object]
    processing_metadata: dict[str, object]
    errors: list[str]


class ContractProcessingState(PreparedContractState, total=False):
    """生产模式固定四类产物的状态。"""

    attribute_result: list[dict[str, object]]
    clause_result: list[dict[str, object]]
    abstract_result: dict[str, object]


class FieldDiscoveryState(PreparedContractState, total=False):
    """发现模式保留 Core、固定 Attribute 上下文和字段候选。"""

    attribute_result: list[dict[str, object]]
    field_candidates: list[dict[str, object]]
    discovery_metrics: dict[str, object]


class FieldDiscoveryBatchState(TypedDict, total=False):
    """批次父图只传递阶段产物，不泄漏每份合同的可变运行资源。"""

    contract_paths: list[Path]
    batch_documents: list[dict[str, Any]]
    stage_one: dict[str, Any]
    stage_two: dict[str, Any]


class FieldDiscoveryStageTwoState(TypedDict, total=False):
    """第二阶段动态并发任务与 reducer 汇合状态。"""

    extraction_tasks: list[dict[str, Any]]
    extraction_task: dict[str, Any]
    observations: Annotated[list[dict[str, Any]], operator.add]
    stage_two: dict[str, Any]
