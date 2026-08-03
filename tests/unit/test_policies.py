"""领域频次规则的回归测试。"""

import unittest

from contract_processor.domain.models import AttributeStatistics
from contract_processor.domain.policies import record_attribute_occurrence


class AttributeStatisticsTests(unittest.TestCase):
    def test_contract_count_deduplicates_same_contract(self) -> None:
        statistics = record_attribute_occurrence(
            AttributeStatistics(), document_id="a" * 64, round_id="round-1"
        )
        statistics = record_attribute_occurrence(
            statistics, document_id="a" * 64, round_id="round-1"
        )

        self.assertEqual(statistics.occurrence_count, 2)
        self.assertEqual(statistics.contract_count, 1)
        self.assertEqual(statistics.first_seen_round, "round-1")
