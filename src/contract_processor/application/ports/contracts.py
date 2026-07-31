"""应用层依赖的外部能力协议。"""

from pathlib import Path
from typing import Any, Protocol, Sequence

from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import FieldDefinition, FieldObservation


class FieldCatalog(Protocol):
    """加载字段库，屏蔽 YAML 等具体存储方式。"""

    def load(self, kind: FieldKind) -> Sequence[FieldDefinition]:
        """返回指定字段库的全部字段定义。"""


class VisionModelClient(Protocol):
    """面向视觉语言模型的最小调用接口。"""

    def generate_json(self, *, prompt: str, image_paths: Sequence[Path]) -> dict[str, Any]:
        """根据合同页图像生成符合调用方 schema 的 JSON。"""


class FieldSimilaritySearcher(Protocol):
    """对候选字段召回语义相近的字段定义。"""

    def search(self, summary: str, *, limit: int) -> Sequence[FieldDefinition]:
        """按相关度降序返回字段候选。"""


class ContractWorkflow(Protocol):
    """供 CLI 和未来 API 共同调用的合同处理工作流。"""

    def process(self, contract_path: Path) -> Sequence[FieldObservation]:
        """处理单份合同并返回其字段观察结果。"""
