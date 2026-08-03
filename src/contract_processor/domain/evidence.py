"""原文证据的通用、无业务关键词比较规则。"""

from __future__ import annotations

import unicodedata


def canonical_evidence_text(value: str) -> str:
    """仅忽略排版空白和 Unicode 标点，保留其余字符及原有顺序。

    合同扫描件常把双语标题的冒号、换行或空格识别得不一致。这里不维护标点词表，
    也不改写最终原文，只生成用于证据包含关系校验的比较副本。
    """

    return "".join(
        character.casefold()
        for character in value
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


def source_contains_span(source_text: str, span_text: str) -> bool:
    """判断候选片段是否按原字符顺序存在于原文，允许排版标点差异。"""

    source = canonical_evidence_text(source_text)
    span = canonical_evidence_text(span_text)
    return bool(span) and span in source
