"""运行模式的纯领域约束。"""

from __future__ import annotations

from contract_processor.domain.enums import FieldKind, RuntimeMode
from contract_processor.domain.models import FieldCatalogSnapshot


class RuntimeConfigurationError(ValueError):
    """运行模式与字段目录组合不满足业务不变量。"""


def validate_core_catalog_for_mode(
    mode: RuntimeMode, snapshot: FieldCatalogSnapshot
) -> None:
    """在渲染 PDF 或连接模型前校验 Core 启动条件。"""

    if snapshot.kind is not FieldKind.CORE:
        raise RuntimeConfigurationError("运行模式校验只接受 Core 字段目录。")
    if mode is RuntimeMode.PRODUCTION and snapshot.is_empty:
        raise RuntimeConfigurationError(
            "生产模式必须配置至少一个 Core 字段，禁止以 0 Core 启动。"
        )
    if mode is RuntimeMode.PRODUCTION and snapshot.field_count == 0:
        # 目录适配器原则上已拒绝状态与字段数量不一致；这里保留应用边界硬门禁。
        raise RuntimeConfigurationError(
            "生产模式必须配置至少一个 Core 字段，当前目录没有可用定义。"
        )
