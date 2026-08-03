"""根据原始 PDF 文件字节生成稳定文档身份。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from contract_processor.async_utils import run_blocking
from contract_processor.domain.identifiers import validate_document_id


READ_CHUNK_BYTES = 1024 * 1024


async def compute_document_id(pdf_path: Path) -> str:
    """流式计算原始文件 SHA-256；不使用渲染图像、文件名或合同字段。"""

    return await run_blocking(_compute_document_id_sync, pdf_path)


def _compute_document_id_sync(pdf_path: Path) -> str:
    """供工作线程调用的流式哈希实现。"""

    digest = hashlib.sha256()
    with pdf_path.open("rb") as source:
        while chunk := source.read(READ_CHUNK_BYTES):
            digest.update(chunk)
    return validate_document_id(digest.hexdigest())


class Sha256DocumentIdentityProvider:
    """应用端口适配器：使用原始 PDF 字节的 SHA-256。"""

    async def compute(self, source_pdf: Path) -> str:
        return await compute_document_id(source_pdf)
