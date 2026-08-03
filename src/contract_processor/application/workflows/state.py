"""两种工作流各自使用的框架无关状态协议。"""

from pathlib import Path
from typing import TypedDict


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
    """发现模式只包含 Core 上下文和字段候选。"""

    field_candidates: list[dict[str, object]]
