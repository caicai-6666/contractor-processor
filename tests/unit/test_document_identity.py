"""原始 PDF 文件身份计算回归测试。"""

import asyncio
import hashlib
from pathlib import Path

from contract_processor.infrastructure.pdf.document_identity import compute_document_id


def test_document_id_is_sha256_of_exact_file_bytes(tmp_path: Path) -> None:
    pdf_path = tmp_path / "contract.pdf"
    source_bytes = b"%PDF-1.7\ncontract bytes\x00\xff"
    pdf_path.write_bytes(source_bytes)

    document_id = asyncio.run(compute_document_id(pdf_path))

    assert document_id == hashlib.sha256(source_bytes).hexdigest()
    assert len(document_id) == 64
    assert document_id == document_id.lower()


def test_document_id_changes_when_original_file_bytes_change(tmp_path: Path) -> None:
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(b"first")
    first_id = asyncio.run(compute_document_id(pdf_path))
    pdf_path.write_bytes(b"second")

    assert asyncio.run(compute_document_id(pdf_path)) != first_id
