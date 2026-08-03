"""抽取阶段在内存中传递的业务结果与校验信息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar


PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True, slots=True)
class StageResult(Generic[PayloadT]):
    """避免正式抽取流程通过临时文件传递结果或校验状态。"""

    payload: PayloadT
    validation: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    # 仅在同一次处理的内部阶段间传递，不进入应用层结果 DTO 或任何持久化对象。
    artifacts: dict[str, Any] = field(default_factory=dict)
