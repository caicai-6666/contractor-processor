"""LlamaIndex 字段召回适配器的预留位置。"""

from collections.abc import Sequence

from contract_processor.domain.models import FieldDefinition


class LlamaIndexFieldSimilaritySearcher:
    """后续将 LlamaIndex Node 检索结果转换为 FieldDefinition。"""

    def search(self, summary: str, *, limit: int) -> Sequence[FieldDefinition]:
        # 召回索引依赖嵌入模型与持久化策略，待首轮字段样本产生后再初始化。
        raise NotImplementedError("字段向量索引尚未初始化")
