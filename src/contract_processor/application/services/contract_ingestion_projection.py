"""终审包络清洗、我方主体识别与向量输入投影。"""

from __future__ import annotations

from copy import deepcopy
import json
import re
import unicodedata
from typing import Any, Iterable

from contract_processor.application.schemas.contract_ingestion import (
    CleaningMetrics,
    ContractReviewConfirmation,
    ContractSearchProjection,
)


OWN_COMPANY_NAMES_ENV = "CONTRACT_PROCESSOR_OWN_COMPANY_NAMES"


def parse_own_company_names(
    raw_value: str | None,
    *,
    env_name: str = OWN_COMPANY_NAMES_ENV,
) -> tuple[str, ...]:
    """严格解析环境变量，避免错误配置悄悄把我方写成对方。"""

    if raw_value is None:
        raise ValueError(f"缺少环境变量 {env_name}。")
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{env_name} 必须是 JSON 数组。") from error
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{env_name} 必须是非空 JSON 数组。")
    if any(not isinstance(item, str) or not item.strip() for item in payload):
        raise ValueError(f"{env_name} 只能包含非空字符串。")
    return tuple(dict.fromkeys(item.strip() for item in payload))


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def _is_ascii_alias(value: str) -> bool:
    return bool(value) and all(ord(character) < 128 for character in value)


def is_own_company_name(candidate: str, own_company_names: Iterable[str]) -> bool:
    """中文简称允许包含匹配；英文别名使用字母数字边界避免误命中。"""

    normalized_candidate = _normalized_name(candidate)
    compact_candidate = normalized_candidate.replace(" ", "")
    for alias in own_company_names:
        normalized_alias = _normalized_name(alias)
        if _is_ascii_alias(normalized_alias):
            pattern = rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])"
            if re.search(pattern, normalized_candidate):
                return True
        elif normalized_alias.replace(" ", "") in compact_candidate:
            return True
    return False


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def _prune_empty(value: Any) -> Any:
    """递归删除空值，同时保留 false 和数值 0。"""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            pruned = _prune_empty(item)
            if not _is_empty(pruned):
                cleaned[key] = pruned
        return cleaned
    if isinstance(value, list):
        cleaned_items: list[Any] = []
        for item in value:
            pruned = _prune_empty(item)
            if not _is_empty(pruned):
                cleaned_items.append(pruned)
        return cleaned_items
    if isinstance(value, str):
        return value.strip()
    return value


def _clean_field_envelope(envelope: Any) -> tuple[dict[str, Any] | None, Any]:
    """最终 value 无效时删除整个字段；对象字段递归保留有效子字段。"""

    if not isinstance(envelope, dict):
        return None, None
    properties = envelope.get("properties")
    if isinstance(properties, dict):
        cleaned_properties: dict[str, Any] = {}
        values: dict[str, Any] = {}
        for name, child in properties.items():
            cleaned_child, child_value = _clean_field_envelope(child)
            if cleaned_child is not None:
                cleaned_properties[name] = cleaned_child
                values[name] = child_value
        if not cleaned_properties:
            return None, None
        cleaned_parent = _prune_empty(deepcopy(envelope))
        cleaned_parent["status"] = "found"
        cleaned_parent["properties"] = cleaned_properties
        return cleaned_parent, values

    cleaned_value = _prune_empty(envelope.get("value"))
    if _is_empty(cleaned_value):
        return None, None
    cleaned_envelope = _prune_empty(deepcopy(envelope))
    cleaned_envelope["status"] = "found"
    cleaned_envelope["value"] = cleaned_value
    return cleaned_envelope, cleaned_value


def _clean_core(core: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned: dict[str, Any] = {}
    values: dict[str, Any] = {}
    for field_id, envelope in core.items():
        cleaned_envelope, value = _clean_field_envelope(envelope)
        if cleaned_envelope is not None:
            cleaned[field_id] = cleaned_envelope
            values[field_id] = value
    return cleaned, values


def _clean_attributes(
    attributes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    for attribute in attributes:
        field_id = attribute.get("field_id")
        if not isinstance(field_id, str) or not field_id.strip():
            raise ValueError("Attribute 缺少有效 field_id。")
        if field_id in values:
            raise ValueError(f"Attribute field_id 重复：{field_id}")
        cleaned_envelope, value = _clean_field_envelope(attribute)
        if cleaned_envelope is None:
            continue
        cleaned_envelope["field_id"] = field_id
        cleaned.append(cleaned_envelope)
        values[field_id] = value
    return cleaned, values


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        rendered = value.strip()
        key = _normalized_name(rendered)
        if rendered and key not in seen:
            result.append(rendered)
            seen.add(key)
    return tuple(result)


def _counterparties(
    parties: Any, own_company_names: tuple[str, ...]
) -> tuple[tuple[str, ...], str]:
    if not isinstance(parties, list) or not parties:
        return (), "unavailable"
    candidates: list[str] = []
    own_matches = 0
    for party in parties:
        if not isinstance(party, dict):
            continue
        name = party.get("normalized_name") or party.get("source_name")
        if not isinstance(name, str) or not name.strip():
            continue
        if is_own_company_name(name, own_company_names):
            own_matches += 1
        else:
            candidates.append(name)
    if own_matches == 0:
        return (), "unresolved"
    return _deduplicate(candidates), "resolved"


def _product_names(subject_matter: Any) -> tuple[str, ...]:
    if not isinstance(subject_matter, dict):
        return ()
    candidates: list[str] = []
    items = subject_matter.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("normalized_name") or item.get("source_name")
            if isinstance(name, str):
                candidates.append(name)
    if not candidates:
        summary = subject_matter.get("summary")
        if isinstance(summary, str):
            candidates.append(summary)
    return _deduplicate(candidates)


def build_contract_search_projection(
    confirmation: ContractReviewConfirmation,
    *,
    own_company_names: tuple[str, ...],
) -> ContractSearchProjection:
    """从完整终审包络生成清洗结果、检索投影和向量输入。"""

    result = confirmation.result.model_dump(mode="json")
    core = result.get("core", {})
    attributes = result.get("attribute", [])
    if not isinstance(core, dict) or not isinstance(attributes, list):
        raise ValueError("终审结果的 core/attribute 结构无效。")
    cleaned_core, core_values = _clean_core(core)
    cleaned_attributes, attribute_values = _clean_attributes(attributes)

    kept_attribute_ids = {item["field_id"] for item in cleaned_attributes}
    source_attribute_ids = {
        item.get("field_id")
        for item in attributes
        if isinstance(item, dict) and isinstance(item.get("field_id"), str)
    }
    metrics = CleaningMetrics(
        core_before=len(core),
        core_after=len(cleaned_core),
        attribute_before=len(attributes),
        attribute_after=len(cleaned_attributes),
        removed_core_fields=tuple(sorted(set(core) - set(cleaned_core))),
        removed_attribute_fields=tuple(sorted(source_attribute_ids - kept_attribute_ids)),
    )

    contract_name = core_values.get("contract_title")
    if not isinstance(contract_name, str):
        contract_name = None
    counterparties, resolution_status = _counterparties(
        core_values.get("contract_parties"), own_company_names
    )
    products = _product_names(core_values.get("subject_matter"))
    abstract = _prune_empty(result.get("abstract", {}))
    abstract_text = abstract.get("text") if isinstance(abstract, dict) else None
    if not isinstance(abstract_text, str) or not abstract_text.strip():
        raise ValueError("终审结果缺少可向量化的 Abstract 正文。")

    reviewed_result = {
        "document_id": result["document_id"],
        "source_name": result["source_name"],
        "core": cleaned_core,
        "attribute": cleaned_attributes,
        "clause": _prune_empty(result.get("clause", [])),
        "abstract": abstract,
        "processing": _prune_empty(result.get("processing", {})),
    }
    document: dict[str, Any] = {
        "document_id": confirmation.document_id,
        "source_name": result["source_name"],
        "review": confirmation.review.model_dump(mode="json"),
        "reviewed_result": reviewed_result,
        "core_values": core_values,
        "attribute_values": attribute_values,
        "clause": reviewed_result["clause"],
        "abstract": abstract,
        "processing": reviewed_result["processing"],
        "counterparty_resolution_status": resolution_status,
    }
    if contract_name:
        document["contract_name"] = contract_name
    if counterparties:
        document["counterparty_names"] = list(counterparties)
    if products:
        document["product_names"] = list(products)
    return ContractSearchProjection(
        document=document,
        contract_name=contract_name,
        counterparty_names=counterparties,
        product_names=products,
        abstract_text=abstract_text.strip(),
        counterparty_resolution_status=resolution_status,
        metrics=metrics,
    )
