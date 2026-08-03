"""兼容导出；正式入库结果模型位于 application.schemas。"""

from contract_processor.application.schemas.contract_ingestion import (
    CleaningMetrics,
    ContractIngestionOutcome as IngestionOutcome,
    ContractSearchProjection as SearchProjection,
)

__all__ = ["CleaningMetrics", "IngestionOutcome", "SearchProjection"]
