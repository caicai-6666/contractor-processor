"""应用层依赖的外部能力协议。"""

from pathlib import Path
from typing import Any, Protocol, Sequence

from contract_processor.application.schemas.field_discovery import (
    FieldDiscoveryBatchResult,
    FieldDiscoveryOutput,
    FieldDiscoveryRequest,
)
from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import (
    FieldCatalogSnapshot,
    FieldDefinition,
    FieldObservation,
)


class FieldCatalog(Protocol):
    """加载字段库，屏蔽 YAML 等具体存储方式。"""

    async def load(self, kind: FieldKind) -> Sequence[FieldDefinition]:
        """返回指定字段库的全部字段定义。"""

    async def snapshot(self, kind: FieldKind) -> FieldCatalogSnapshot:
        """返回带版本和状态的不可变字段目录快照。"""


class VisionModelClient(Protocol):
    """面向视觉语言模型的最小调用接口。"""

    async def generate_json(
        self, *, prompt: str, image_paths: Sequence[Path]
    ) -> dict[str, Any]:
        """根据合同页图像生成符合调用方 schema 的 JSON。"""


class FieldSimilaritySearcher(Protocol):
    """对候选字段召回语义相近的字段定义。"""

    async def search(
        self, summary: str, *, limit: int
    ) -> Sequence[FieldDefinition]:
        """按相关度降序返回字段候选。"""


class FieldSummaryEmbeddingClient(Protocol):
    """为批次内字段定义和候选摘要生成同空间向量。"""

    async def embed_field_summary(self, summary: str) -> Sequence[float]:
        """返回单个字段摘要向量；调用方不持久化该向量。"""


class FieldDiscoveryService(Protocol):
    """开放字段发现算法的应用端口；本阶段只定义协议。"""

    async def discover(self, request: FieldDiscoveryRequest) -> FieldDiscoveryOutput:
        """从原始合同和已知字段空间生成待治理候选。"""


class FieldDiscoveryResultStore(Protocol):
    """持久化完成回扫与统计的字段发现批次。"""

    async def save(self, result: FieldDiscoveryBatchResult) -> Path:
        """保存批次结果，并返回可供操作人员定位的产物路径。"""


class ContractWorkflow(Protocol):
    """供 CLI 和未来 API 共同调用的合同处理工作流。"""

    async def process(self, contract_path: Path) -> Sequence[FieldObservation]:
        """处理单份合同并返回其字段观察结果。"""
