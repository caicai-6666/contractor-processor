"""查看字段库状态的轻量用例。"""

from dataclasses import dataclass

from contract_processor.application.ports.contracts import FieldCatalog
from contract_processor.domain.enums import FieldKind


@dataclass(frozen=True, slots=True)
class FieldCatalogSummary:
    core_count: int
    attribute_count: int


class InspectFieldCatalog:
    """为本地开发提供字段库连通性检查。"""

    def __init__(self, catalog: FieldCatalog) -> None:
        self._catalog = catalog

    async def execute(self) -> FieldCatalogSummary:
        return FieldCatalogSummary(
            core_count=len(await self._catalog.load(FieldKind.CORE)),
            attribute_count=len(await self._catalog.load(FieldKind.ATTRIBUTE)),
        )
