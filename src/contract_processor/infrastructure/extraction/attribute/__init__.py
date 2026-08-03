"""Attribute 正式抽取实现。"""

from contract_processor.infrastructure.extraction.attribute.empty_service import (
    EmptyAttributeExtractionService,
)
from contract_processor.infrastructure.extraction.attribute.service import (
    AttributeExtractionService,
)

__all__ = ["AttributeExtractionService", "EmptyAttributeExtractionService"]
