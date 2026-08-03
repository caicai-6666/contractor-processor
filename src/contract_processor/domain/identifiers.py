"""合同文档身份的领域约束。"""

from __future__ import annotations

import re


SHA256_DOCUMENT_ID_PATTERN = r"^[0-9a-f]{64}$"
_SHA256_DOCUMENT_ID = re.compile(SHA256_DOCUMENT_ID_PATTERN)


def validate_document_id(document_id: str) -> str:
    """要求文档 ID 为原始文件 SHA-256 的小写十六进制表示。"""

    if not _SHA256_DOCUMENT_ID.fullmatch(document_id):
        raise ValueError("document_id 必须是原始 PDF 文件的 64 位小写 SHA-256")
    return document_id
