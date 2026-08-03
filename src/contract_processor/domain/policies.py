"""不依赖模型或存储的领域规则。"""

from dataclasses import replace

from contract_processor.domain.identifiers import validate_document_id
from contract_processor.domain.models import AttributeStatistics


def record_attribute_occurrence(
    statistics: AttributeStatistics,
    *,
    document_id: str,
    round_id: str,
) -> AttributeStatistics:
    """记录一次发现，并以文档 ID 去重以保证审核频次可信。"""

    validate_document_id(document_id)
    return replace(
        statistics,
        occurrence_count=statistics.occurrence_count + 1,
        document_ids=statistics.document_ids | {document_id},
        first_seen_round=statistics.first_seen_round or round_id,
        last_seen_round=round_id,
    )
