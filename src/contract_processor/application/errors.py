"""应用用例对外暴露的稳定错误类型。"""

from typing import Any


class FieldDiscoveryUnavailableError(RuntimeError):
    """发现模式已构建，但尚未注入具体字段发现实现。"""


class StageValidationError(RuntimeError):
    """阶段门禁拒绝结果时保留内存诊断，供实验适配器精确记录。"""

    def __init__(
        self,
        *,
        stage: str,
        validation: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        super().__init__(f"{stage} 阶段校验未通过，统一工作流拒绝生成成功结果。")
        self.stage = stage
        self.validation = validation
        self.metrics = metrics
