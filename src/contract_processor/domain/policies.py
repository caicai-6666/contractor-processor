"""不依赖模型或存储的领域规则。"""

from dataclasses import replace

from contract_processor.domain.models import AttributeStatistics


def record_attribute_occurrence(
    statistics: AttributeStatistics,
    *,
    contract_id: str,
    round_id: str,
) -> AttributeStatistics:
    """记录一次发现，并以合同 ID 去重以保证审核频次可信。"""

    return replace(
        statistics,
        occurrence_count=statistics.occurrence_count + 1,
        contract_ids=statistics.contract_ids | {contract_id},
        first_seen_round=statistics.first_seen_round or round_id,
        last_seen_round=round_id,
    )
