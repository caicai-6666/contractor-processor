"""Core 正式抽取实现。"""

from contract_processor.infrastructure.extraction.core.empty_service import (
    EmptyCoreExtractionService,
)
from contract_processor.infrastructure.extraction.core.service import CoreExtractionService

__all__ = ["CoreExtractionService", "EmptyCoreExtractionService"]
