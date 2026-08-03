"""专家终审入库用例所依赖的外部能力端口。"""

from pathlib import Path
from typing import Any, Mapping, Protocol

from contract_processor.application.schemas.contract_ingestion import (
    StoredSourceDocument,
)


class ContractEmbeddingClient(Protocol):
    """合同字段和 PDF 视觉向量生成端口。"""

    @property
    def dimensions(self) -> int: ...

    @property
    def model(self) -> str: ...

    @property
    def instruction_version(self) -> str: ...

    @property
    def visual_strategy(self) -> str: ...

    async def probe(self) -> None: ...

    async def embed_text_fields(
        self, inputs: Mapping[str, str]
    ) -> dict[str, list[float]]: ...

    async def embed_pdf(self, pdf_path: Path) -> tuple[list[float], int]: ...

    async def close(self) -> None: ...


class SourceDocumentStore(Protocol):
    """以 document_id 为唯一定位键保存和读取原始 PDF。"""

    async def save(self, source_pdf: Path, document_id: str) -> StoredSourceDocument: ...

    async def resolve(self, document_id: str) -> Path: ...


class DocumentIdentityProvider(Protocol):
    """隔离文件哈希实现，使应用工作流可使用内存替身测试。"""

    async def compute(self, source_pdf: Path) -> str: ...


class ContractIndexRepository(Protocol):
    """正式合同多向量索引端口，不暴露 LlamaIndex 或 ES 类型。"""

    @property
    def index_name(self) -> str: ...

    async def ensure_ready(self) -> str: ...

    async def save(self, document: dict[str, Any]) -> None: ...

    async def get(self, document_id: str) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


class ContractIngestionGraph(Protocol):
    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]: ...


class ContractIngestionGraphFactory(Protocol):
    """应用层只依赖可异步执行的图协议。"""

    def build_contract_ingestion(self, **nodes: Any) -> ContractIngestionGraph: ...
