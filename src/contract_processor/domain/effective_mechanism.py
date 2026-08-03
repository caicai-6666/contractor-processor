"""生效机制的跨子字段业务不变量。"""

from __future__ import annotations

from contract_processor.domain.evidence import source_contains_span


def effective_date_has_provenance(
    *, date_raw_value: str | None, trigger_type: object, trigger_text: str | None
) -> bool:
    """判断规范化生效日期是否具备与触发机制一致的原文来源。

    明确日期以及单纯签订/最后签署触发可按字段规范结合唯一签署日期归一化；签字加
    盖章、付款、审批等条件的完成日期则必须直接出现在生效条款证据中。
    """

    if trigger_type in {"explicit_date", "on_signing", "on_last_signature"}:
        return True
    return bool(
        date_raw_value
        and trigger_text
        and source_contains_span(trigger_text, date_raw_value)
    )
