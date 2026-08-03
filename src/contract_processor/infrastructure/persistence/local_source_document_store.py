"""基于本地文件系统、按 document_id 内容寻址的 PDF 存储。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from uuid import uuid4

from contract_processor.application.schemas.contract_ingestion import (
    StoredSourceDocument,
)
from contract_processor.async_utils import run_blocking
from contract_processor.domain.identifiers import validate_document_id
from contract_processor.infrastructure.pdf.document_identity import (
    _compute_document_id_sync,
)


class LocalSourceDocumentStore:
    """只暴露相对存储键；文件名固定为 ``<document_id>.pdf``。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    async def save(
        self, source_pdf: Path, document_id: str
    ) -> StoredSourceDocument:
        return await run_blocking(self._save_sync, source_pdf, document_id)

    def _save_sync(
        self, source_pdf: Path, document_id: str
    ) -> StoredSourceDocument:
        document_id = validate_document_id(document_id)
        source_pdf = source_pdf.resolve()
        if not source_pdf.is_file():
            raise FileNotFoundError(f"源 PDF 不存在：{source_pdf}")
        if source_pdf.suffix.casefold() != ".pdf":
            raise ValueError(f"源文件不是 PDF：{source_pdf.name}")

        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / f"{document_id}.pdf"
        if target.exists():
            if not target.is_file() or _compute_document_id_sync(target) != document_id:
                raise RuntimeError(
                    f"document_id 对应目标文件已存在但内容不一致：{target}"
                )
            return StoredSourceDocument(
                document_id=document_id,
                storage_key=target.name,
                mime_type="application/pdf",
                size_bytes=target.stat().st_size,
                created=False,
            )

        temporary = self._root / f".{document_id}.{uuid4().hex}.tmp"
        try:
            with source_pdf.open("rb") as source, temporary.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if _compute_document_id_sync(temporary) != document_id:
                raise RuntimeError("PDF 临时副本哈希校验失败，拒绝激活。")
            # 同目录 replace 保证最终文件不会以半写状态被读取。document_id 是内容哈希，
            # 并发提交即使同时到达，也只可能写入内容相同的 PDF。
            temporary.replace(target)
            directory_fd = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredSourceDocument(
            document_id=document_id,
            storage_key=target.name,
            mime_type="application/pdf",
            size_bytes=target.stat().st_size,
            created=True,
        )

    async def resolve(self, document_id: str) -> Path:
        return await run_blocking(self._resolve_sync, document_id)

    def _resolve_sync(self, document_id: str) -> Path:
        document_id = validate_document_id(document_id)
        target = self._root / f"{document_id}.pdf"
        if not target.is_file():
            raise FileNotFoundError(f"合同 PDF 不存在：{document_id}")
        if _compute_document_id_sync(target) != document_id:
            raise RuntimeError(f"合同 PDF 内容哈希校验失败：{document_id}")
        return target
