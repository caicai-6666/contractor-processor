"""合同发现工作流在各节点之间传递的最小状态。"""

from pathlib import Path
from typing import TypedDict


class ContractDiscoveryState(TypedDict, total=False):
    """此状态不依赖 LangGraph，可被其他编排器复用。"""

    contract_path: Path
    core_result: dict[str, object]
    attribute_candidates: list[dict[str, object]]
    errors: list[str]
