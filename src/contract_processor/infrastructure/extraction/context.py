"""正式抽取服务共享的不可变运行上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter


@dataclass(frozen=True, slots=True)
class PdfExtractionContext:
    """封装同一合同在所有子图间复用的资源。

    页面图像和客户端由统一准备阶段创建一次；各算法服务只读取上下文，不负责重新
    渲染 PDF 或重建网络连接，从而保持资源生命周期和业务算法相互独立。
    """

    project_root: Path
    pdf_path: Path
    document_id: str
    images: list[dict[str, Any]]
    source_page_count: int
    client: AsyncOpenAI
    model_request_limiter: ModelRequestLimiter = field(
        default_factory=lambda: ModelRequestLimiter(1)
    )

    def __post_init__(self) -> None:
        if self.source_page_count < 1:
            raise ValueError("source_page_count 必须大于 0。")
        if len(self.images) != self.source_page_count:
            raise ValueError("共享页面图像数量必须与 PDF 页数一致。")
