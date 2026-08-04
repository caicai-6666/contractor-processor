"""字段发现第一阶段的候选生成、向量归并与内存关系图实现。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
import time
from typing import Any, Literal, Sequence

from llama_index.core.schema import TextNode
from llama_index.core.vector_stores.simple import SimpleVectorStore
from llama_index.core.vector_stores.types import VectorStoreQuery
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from contract_processor.application.prompts.pdf_prefix import SYSTEM_MESSAGE
from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import FieldDefinition
from contract_processor.infrastructure.embedding import Qwen3VLEmbeddingClient
from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
from contract_processor.settings import ProjectSettings
from contract_processor.infrastructure.field_discovery.field_description import (
    OUTPUT_DESCRIPTION_PROMPT_RULES,
    OutputDescription,
    compile_output_description,
    field_definition_record,
    render_field_card,
)
from contract_processor.infrastructure.field_discovery.prompt_templates import (
    render_discovery_prompt,
)


FIELD_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SUPPORTED_OUTPUT_TYPES = {
    "string",
    "number",
    "integer",
    "boolean",
    "date",
    "enum",
    "object",
    "array",
}
GENERIC_FIELD_NAMES = {"其他信息", "其他条款", "备注", "补充信息", "关键信息", "杂项"}
DOCUMENT_INTRINSIC_FIELD_IDS = {
    "contract_language",
    "document_language",
    "document_page_count",
    "pdf_page_count",
    "file_format",
    "scan_quality",
    "ocr_quality",
}
PRELIMINARY_AGREEMENT_IDENTITY_PATTERN = re.compile(
    r"preliminary_agreement|precontract|pre_contract|letter_of_intent"
)
PRELIMINARY_AGREEMENT_EVIDENCE_PATTERN = re.compile(
    r"初步协议|预备协议|先行协议|意向书|备忘录|框架协议|草签|预签|"
    r"正式(?:合同|协议).{0,16}(?:另行)?(?:签署|签订)|"
    r"后续.{0,16}(?:签署|签订)|另行(?:签署|签订)"
)
VECTOR_VIEW_WEIGHTS = {"label": 0.30, "meaning": 0.50, "structure": 0.20}
RRF_OFFSET = 60
FIELD_RELATION_SYSTEM_MESSAGE = render_discovery_prompt(
    "00a_relation_system.txt", {}
)
RELATION_REASON_SUFFIX_PATTERN = re.compile(
    r"(?:[。！？.!]\s*)?因此\s*relation\s*=\s*"
    r"(?P<relation>same|related_distinct|unrelated)\s*[。！？.!]?\s*$",
    flags=re.DOTALL,
)
EXTRACTION_RULE_LOCATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"第\s*[零〇一二三四五六七八九十百千万两\d]+\s*(?:页|条|款|章|节)"),
        "不得使用页码、条款号或章节序号定位字段",
    ),
    (
        re.compile(
            r"条款\s*[（(]?\s*[零〇一二三四五六七八九十百千万两\d]+\s*[)）]?"
        ),
        "不得使用‘条款7’或‘条款(7)’一类编号定位字段",
    ),
    (
        re.compile(
            r"(?:从|在|依据|根据).{0,80}(?:条款|章节|段落|部分)"
            r"(?:中|内)?\s*(?:提取|解析|识别|查找|读取|获取|定位)"
        ),
        "不得把当前合同的条款、章节、段落或部分当作固定提取位置",
    ),
    (
        re.compile(
            r"(?:从|在|依据|根据).{0,80}"
            r"(?:首部|首页|末页|尾部|签章页|附件|表格|附加信息|其他约定|其它约定)"
            r".{0,8}(?:提取|解析|识别|查找|读取|获取|定位)"
        ),
        "不得把当前合同的版式位置或章节标题当作固定提取位置",
    ),
)
QUOTED_FORMAT_EXAMPLE_PATTERN = re.compile(
    r"(?:['‘“])(?P<value>[^'’”\r\n]{1,80})(?:['’”])"
)
class StrictModel(BaseModel):
    """所有模型包络拒绝额外键，避免把自由文本误当作字段定义。"""

    model_config = ConfigDict(extra="forbid")


class CandidateEvidence(StrictModel):
    page_number: int = Field(ge=1)
    source_text: str = Field(min_length=6, max_length=1200)


class CandidateProposal(StrictModel):
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=2, max_length=80)
    meaning: str = Field(min_length=10, max_length=500)
    # 第一阶段只建立字段身份；别名、排除概念和真实示例在身份收敛后治理。
    output: OutputDescription
    extraction_rule: str = Field(min_length=10, max_length=800)
    evidence: CandidateEvidence
    # 解释必须先于模型的建议结论输出，方便 JSON 截断或人工阅读时保留依据。
    novelty_reason: str = Field(min_length=10, max_length=1200)
    # 这里只表示模型建议该提议进入程序门禁；最终 accepted/rejected 仍由程序决定。
    status: Literal["accepted"]


class CandidateProposalBatch(StrictModel):
    candidates: list[CandidateProposal] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True)
class CandidateProposalParseFailure:
    """单个候选未通过结构校验时保留其序号和脱离批次后的修复输入。"""

    proposal_index: int
    payload: object
    error: str


CandidateGateStatus = Literal[
    "accepted",
    "covered_by_fixed",
    "non_atomic",
    "not_attribute",
    "invalid_rule",
]


class CandidateSemanticGateDecision(StrictModel):
    """模型只作语义准入判断；候选结构与最终去向仍由程序校验。"""

    proposal_index: int = Field(ge=1, le=5)
    covered_by_field_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=10, max_length=1200)
    status: CandidateGateStatus


class CandidateSemanticGateBatch(StrictModel):
    decisions: list[CandidateSemanticGateDecision] = Field(
        default_factory=list, max_length=5
    )


class ExtractionRuleRevision(StrictModel):
    """允许修订字段与子字段规则，但程序会锁定 output 的其余结构。"""

    extraction_rule: str = Field(min_length=10, max_length=800)
    output: OutputDescription


class ExtractionRuleGeneralizationError(ValueError):
    """字段规则混入当前合同位置证据，无法作为跨合同字段定义。"""


class StructuredModelResponseError(RuntimeError):
    """结构化响应失败；只携带耗时/token 等非敏感指标，不保留模型原文。"""

    def __init__(self, message: str, *, metrics: dict[str, Any]) -> None:
        super().__init__(message)
        self.metrics = metrics


Relation = Literal["same", "related_distinct", "unrelated"]


def finalize_relation_reason(*, reason: str, relation: Relation) -> str:
    """把关系理由规范为可读、可机器核对的固定结尾。

    模型偶尔会只在独立 relation 字段给出最终分类。输出归档前由程序补齐固定结尾；若模型已经
    显式写出相反的结论，则拒绝该响应，避免理由和结构化关系产生可见矛盾。
    """

    normalized = reason.strip()
    suffix = RELATION_REASON_SUFFIX_PATTERN.search(normalized)
    if suffix:
        declared_relation = suffix.group("relation")
        if declared_relation != relation:
            raise ValueError(
                "关系理由的固定结尾与 relation 字段不一致："
                f"reason={declared_relation}，relation={relation}。"
            )
        normalized = normalized[: suffix.start()].rstrip("。！？.! \t\r\n")
    else:
        normalized = normalized.rstrip("。！？.! \t\r\n")
    if not normalized:
        raise ValueError("关系理由必须在最终 relation 结论前说明具体的字段边界。")
    return f"{normalized}。因此 relation={relation}"


class RelationComparison(StrictModel):
    target_candidate_id: str = Field(min_length=1)
    # 理由置于最终关系结论之前，保证模型先完成边界比对再提交三分类。
    reason: str = Field(min_length=4, max_length=1200)
    relation: Relation


class SingleRelationJudgement(StrictModel):
    """单个候选对的模型输出；目标身份由程序绑定，模型无权改写。"""

    reason: str = Field(min_length=4, max_length=1200)
    relation: Relation


class RelationJudgement(StrictModel):
    comparisons: list[RelationComparison] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True, slots=True)
class CandidateMatch:
    candidate_id: str
    group_id: str
    fused_score: float
    best_rank: int
    # 原始相似度与各视角名次用于后续基于人工标注校准阈值；RRF 分数本身不是 0~1 概率。
    view_scores: dict[str, float] = field(default_factory=dict)
    view_ranks: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RelationPromptParts:
    """同一当前候选的稳定文本前缀与单个目标候选的可变后缀。"""

    preamble: str
    target: str


@dataclass(frozen=True, slots=True)
class CandidateProposalRecord:
    """通过结构门禁后的字段提议；证据原文只在本对象生命周期内存在。"""

    definition: FieldDefinition
    definition_record: dict[str, Any]
    novelty_reason: str
    evidence_page_number: int
    evidence_text: str

    @property
    def evidence_hash(self) -> str:
        return sha256(self.evidence_text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class CandidateIdentity:
    candidate_id: str
    group_id: str
    proposal: CandidateProposalRecord
    document_ids: set[str] = field(default_factory=set)
    occurrence_count: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, *, document_id: str, page_number: int, evidence_hash: str) -> None:
        self.occurrence_count += 1
        self.document_ids.add(document_id)
        self.observations.append(
            {
                "document_id": document_id,
                "page_number": page_number,
                "evidence_hash": evidence_hash,
            }
        )

    def report(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "group_id": self.group_id,
            "suggested_definition": self.proposal.definition_record,
            "statistics": {
                "occurrence_count": self.occurrence_count,
                "contract_count": len(self.document_ids),
                "document_ids": sorted(self.document_ids),
            },
            "observations": list(self.observations),
        }


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _render_fixed_catalog(label: str, definitions: Sequence[FieldDefinition]) -> str:
    if not definitions:
        return f"【{label}】\n- 目录为空。"
    cards = [render_field_card(field_definition_record(definition)) for definition in definitions]
    return f"【{label}】\n" + "\n\n".join(cards)


def _status_of(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    status = value.get("status")
    return str(status) if isinstance(status, str) else "unknown"


def render_core_status_context(
    fields: dict[str, Any], definitions: Sequence[FieldDefinition]
) -> str:
    """只展示状态，不把合同字段值写入候选发现实验产物。"""

    if not definitions:
        return "【固定 Core 提取状态】\n- Core 目录为空。"
    lines = ["【固定 Core 提取状态】"]
    for definition in definitions:
        status = _status_of(fields.get(definition.field_id))
        lines.append(f"- {definition.name}（{definition.field_id}）：{status}")
    return "\n".join(lines)


def render_attribute_status_context(
    fields: Sequence[dict[str, Any]], definitions: Sequence[FieldDefinition]
) -> str:
    if not definitions:
        return "【固定 Attribute 提取状态】\n- Attribute 目录为空。"
    values = {
        str(item.get("field_id")): item
        for item in fields
        if isinstance(item, dict) and isinstance(item.get("field_id"), str)
    }
    lines = ["【固定 Attribute 提取状态】"]
    for definition in definitions:
        status = _status_of(values.get(definition.field_id))
        lines.append(f"- {definition.name}（{definition.field_id}）：{status}")
    return "\n".join(lines)


def _normalise_term(value: str) -> str:
    return re.sub(r"[\s_\-()（）【】\[\]{}]", "", value).lower()


def _terms(definition: FieldDefinition) -> set[str]:
    return {
        normalised
        for item in (definition.name, *definition.aliases)
        if (normalised := _normalise_term(item))
    }


def _validate_output_definition(output: Any, *, path: str = "output") -> None:
    """在模型生成后执行递归结构门禁，防止不完整定义进入向量池。"""

    if not isinstance(output, dict):
        raise ValueError(f"{path} 必须是对象。")
    output_type = output.get("type")
    if output_type not in SUPPORTED_OUTPUT_TYPES:
        raise ValueError(f"{path}.type 不支持：{output_type!r}")
    common_keys = {
        "type",
        "nullable",
        "format",
        "example",
        "name",
        "meaning",
        "unit",
        "not_meaning",
        "extraction_rule",
        "minimum",
        "maximum",
        "pattern",
        "min_items",
        "max_items",
        "min_length",
        "max_length",
    }
    type_keys = {
        "object": {"properties", "required", "additional_properties"},
        "array": {"items"},
        "enum": {"values"},
    }.get(str(output_type), set())
    unknown_keys = sorted(set(output) - common_keys - type_keys)
    if unknown_keys:
        raise ValueError(f"{path} 包含不支持的键：{unknown_keys}。")
    if not isinstance(output.get("nullable"), bool):
        raise ValueError(f"{path}.nullable 必须显式为布尔值。")
    pattern = output.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise ValueError(f"{path}.pattern 必须是字符串。")
        try:
            compiled_pattern = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"{path}.pattern 不是合法正则：{error}。") from error
        output_format = output.get("format")
        if isinstance(output_format, str):
            literal_examples = [
                match.group("value")
                for match in QUOTED_FORMAT_EXAMPLE_PATTERN.finditer(output_format)
                # X/YY 等格式占位符不是模型承诺的字面规范值。
                if not re.search(r"[XxYy*…]", match.group("value"))
            ]
            rejected_examples = [
                example
                for example in literal_examples
                if compiled_pattern.fullmatch(example) is None
            ]
            if rejected_examples:
                raise ValueError(
                    f"{path}.format 明确列出的字面示例未通过同层 pattern："
                    f"{rejected_examples}。请统一格式说明与正则约束。"
                )
    nested_rule = output.get("extraction_rule")
    if nested_rule is not None:
        if not isinstance(nested_rule, str):
            raise ValueError(f"{path}.extraction_rule 必须是字符串。")
        try:
            validate_generalized_extraction_rule(nested_rule)
        except ExtractionRuleGeneralizationError as error:
            raise ExtractionRuleGeneralizationError(
                f"{path}.extraction_rule 未通过规则：{error}"
            ) from error
    if output_type == "object":
        properties = output.get("properties")
        required = output.get("required")
        if not isinstance(properties, dict) or not properties:
            raise ValueError(f"{path}.properties 必须是非空对象。")
        if not isinstance(required, list) or set(required) != set(properties):
            raise ValueError(f"{path}.required 必须恰好覆盖所有直属 properties。")
        if output.get("additional_properties", False) is not False:
            raise ValueError(f"{path}.additional_properties 必须为 false。")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{path}.properties 包含非法名称。")
            _validate_output_definition(child, path=f"{path}.properties.{name}")
    elif output_type == "array":
        if not isinstance(output.get("items"), dict):
            raise ValueError(f"{path}.items 必须是完整递归输出定义。")
        _validate_output_definition(output["items"], path=f"{path}.items")
    elif output_type == "enum":
        values = output.get("values")
        if not isinstance(values, (dict, list)) or not values:
            raise ValueError(f"{path}.values 必须是非空枚举定义。")


def validate_generalized_extraction_rule(rule: str) -> str:
    """拒绝把本合同证据位置写入字段库级提取规则。

    数字本身可能是合法的归一化口径，例如“百分比转为小数”，因此这里只拦截明确的页码、
    条款序号和版式定位句式。模型失败后由调用方携带此处的语义原因重试，程序不静默删词。
    """

    normalized = rule.strip()
    for pattern, reason in EXTRACTION_RULE_LOCATION_PATTERNS:
        match = pattern.search(normalized)
        if match:
            raise ExtractionRuleGeneralizationError(
                f"extraction_rule 泛化性不足：{reason}；命中表达={match.group(0)!r}。"
                "请改写为跨合同适用的确认条件、排除边界、规范化方式及缺失/冲突处理规则。"
            )
    return normalized


def validate_definition_business_consistency(record: dict[str, Any]) -> None:
    """拒绝少量可由定义自身直接证明的业务边界矛盾。

    这里不尝试用字符规则替代开放式语义判断，只覆盖两类确定冲突：付款阶段把
    预付款列为正例后又整体排除，以及已经排除分期付款的付款方式又把分期支付
    列为可采纳模式。其他语义关系仍交给候选门禁与组级模型判断。
    """

    field_id = str(record.get("field_id", ""))
    name = str(record.get("name", ""))
    output = record.get("output")
    rules: list[str] = []

    def collect_rules(value: Any) -> None:
        if not isinstance(value, dict):
            return
        rule = value.get("extraction_rule")
        if isinstance(rule, str):
            rules.append(rule)
        properties = value.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                collect_rules(child)
        collect_rules(value.get("items"))

    top_rule = record.get("extraction_rule")
    if isinstance(top_rule, str):
        rules.append(top_rule)
    collect_rules(output)
    combined_rules = "\n".join(rules)

    def validate_enum_examples(value: Any, *, path: str) -> None:
        if not isinstance(value, dict):
            return
        if value.get("type") == "enum":
            enum_values = value.get("values")
            if isinstance(enum_values, dict):
                normalized_values = {
                    _normalise_term(str(item))
                    for pair in enum_values.items()
                    for item in pair
                }
            elif isinstance(enum_values, list):
                normalized_values = {
                    _normalise_term(str(item)) for item in enum_values
                }
            else:
                normalized_values = set()
            rule = value.get("extraction_rule")
            if isinstance(rule, str):
                quoted_examples = [
                    match.group("value").strip()
                    for match in QUOTED_FORMAT_EXAMPLE_PATTERN.finditer(rule)
                ]
                uncovered = [
                    example
                    for example in quoted_examples
                    if not any(
                        _normalise_term(example) in enum_value
                        or enum_value in _normalise_term(example)
                        for enum_value in normalized_values
                    )
                ]
                if uncovered:
                    raise ValueError(
                        f"{path} 的 extraction_rule 明示了枚举示例 {uncovered}，"
                        "但 output.values 无法表示；请补全封闭集合或改用开放 string。"
                    )
        properties = value.get("properties")
        if isinstance(properties, dict):
            for child_id, child in properties.items():
                validate_enum_examples(child, path=f"{path}.properties.{child_id}")
        validate_enum_examples(value.get("items"), path=f"{path}.items")

    validate_enum_examples(output, path="output")

    is_payment_schedule = field_id == "payment_schedule" or any(
        term in name for term in ("付款安排", "付款计划", "付款阶段")
    )
    if is_payment_schedule and re.search(
        r"(?:排除|不包括|不包含|不得(?:提取|包含)?)"
        r"[^。；\n]{0,40}(?:预付款|首付款)",
        combined_rules,
    ):
        raise ValueError(
            "付款安排不得把预付款或首付款整体排除：它们是合法付款阶段；"
            "只能排除与交易付款无关的保证金、违约金等事实。"
        )

    not_meaning = record.get("not_meaning")
    excludes_payment_schedule = isinstance(not_meaning, list) and any(
        isinstance(value, str)
        and any(term in value for term in ("分期付款", "付款安排", "付款计划"))
        for value in not_meaning
    )
    is_payment_method = field_id == "payment_method" or any(
        term in name for term in ("付款方式", "支付方式")
    )
    positive_text = "\n".join(
        value
        for value in (record.get("meaning"), top_rule)
        if isinstance(value, str)
    )
    if (
        is_payment_method
        and excludes_payment_schedule
        and re.search(
            r"(?:包括|包含|例如|如|特殊结算机制)[^。；\n]{0,30}"
            r"分期(?:支付|付款)",
            positive_text,
        )
    ):
        raise ValueError(
            "付款方式已将分期付款安排列入 not_meaning，meaning/extraction_rule "
            "不得再把分期支付作为可采纳方式；请只保留独立的支付工具或结算机制。"
        )

    is_payment_condition_container = field_id in {
        "payment_term_conditions",
        "payment_conditions",
    } or name in {"付款条件", "支付条件"}
    meaning = str(record.get("meaning", ""))
    if (
        is_payment_condition_container
        and re.search(r"触发|前置条件", meaning)
        and re.search(r"期限|时限|截止", meaning)
        and re.search(r"逾期|后果|违约", meaning)
    ):
        raise ValueError(
            "付款触发事件、付款期限与逾期后果可以独立缺失和治理，"
            "不得打包为一个‘付款条件’候选；应复用付款安排或拆成原子候选。"
        )


def _without_extraction_rules(value: Any) -> Any:
    """递归移除规则文本，用于证明局部重试没有修改字段值结构。"""

    if isinstance(value, dict):
        return {
            key: _without_extraction_rules(child)
            for key, child in value.items()
            if key != "extraction_rule"
        }
    if isinstance(value, list):
        return [_without_extraction_rules(child) for child in value]
    return value


def _extraction_rule_paths(value: Any, *, path: str = "output") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        if "extraction_rule" in value:
            paths.add(f"{path}.extraction_rule")
        for key, child in value.items():
            if key != "extraction_rule":
                paths.update(_extraction_rule_paths(child, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.update(_extraction_rule_paths(child, path=f"{path}[{index}]"))
    return paths


def validate_extraction_rule_revision(
    *, original_output: OutputDescription, revised_output: OutputDescription
) -> OutputDescription:
    """只接受规则文本变化；类型、属性、枚举、单位和其他输出约束必须逐项保持。"""

    original_payload = original_output.model_dump(mode="json", exclude_none=True)
    revised_payload = revised_output.model_dump(mode="json", exclude_none=True)
    if _without_extraction_rules(original_payload) != _without_extraction_rules(
        revised_payload
    ):
        raise ValueError(
            "extraction_rule 局部重试不得修改 output 类型、属性、枚举、单位或其他结构约束。"
        )
    if _extraction_rule_paths(original_payload) != _extraction_rule_paths(revised_payload):
        raise ValueError(
            "extraction_rule 局部重试不得新增或删除 output 子字段规则，只能改写原有规则文本。"
        )
    _validate_output_definition(compile_output_description(revised_output))
    return revised_output


def build_extraction_rule_revision_prompt(
    *, proposal: CandidateProposal, validation_error: str
) -> str:
    """构造无 PDF 的局部纠错任务；字段身份、结构和证据均由程序保持不变。"""

    field_context = {
        "field_id": proposal.field_id,
        "name": proposal.name,
        "meaning": proposal.meaning,
        "output": proposal.output.model_dump(mode="json", exclude_none=True),
        "current_extraction_rule": proposal.extraction_rule,
    }
    return render_discovery_prompt(
        "05_revise_extraction_rule.txt",
        {
            "{{VALIDATION_ERROR}}": validation_error,
            "{{FIELD_CONTEXT}}": _compact_json(field_context),
        },
    )


def build_candidate_proposal_repair_prompt(
    *,
    proposal_payload: object,
    validation_error: str,
    lock_identity: bool = False,
) -> str:
    """构造单候选结构修复任务；不重读 PDF，也不要求重新发现其他候选。"""

    if lock_identity:
        repair_scope = (
            "- 当前候选已经通过 JSON Schema；field_id、name、meaning、evidence、"
            "novelty_reason 和 status 必须逐项保持不变。\n"
            "- 只允许根据错误修正 output 与 extraction_rule；程序会拒绝任何字段身份或证据漂移。"
        )
    else:
        repair_scope = (
            "- 根据程序校验错误修复 field_id、字段描述、output 或 extraction_rule 的格式与结构。"
        )
    return render_discovery_prompt(
        "04_repair_candidate.txt",
        {
            "{{OUTPUT_DESCRIPTION_RULES}}": OUTPUT_DESCRIPTION_PROMPT_RULES,
            "{{REPAIR_SCOPE}}": repair_scope,
            "{{VALIDATION_ERROR}}": validation_error,
            "{{PROPOSAL_PAYLOAD}}": _compact_json(proposal_payload),
        },
    )


def build_single_candidate_semantic_gate_prompt(
    *,
    candidate_index: int,
    candidate: CandidateProposalRecord,
    fixed_definitions: Sequence[FieldDefinition],
) -> str:
    """构造单候选语义准入 Prompt，避免单项输出错误阻塞同合同其他候选。"""

    return render_discovery_prompt(
        "03_admit_candidate.txt",
        {
            "{{PROPOSAL_INDEX}}": str(candidate_index),
            "{{FIXED_FIELD_DEFINITIONS}}": _render_fixed_catalog(
                "固定 Core/Attribute（完整覆盖空间）", fixed_definitions
            ),
            "{{FIXED_FIELD_IDS}}": _compact_json(
                [definition.field_id for definition in fixed_definitions]
            ),
            "{{CANDIDATE_DEFINITION}}": (
                f"候选序号：{candidate_index}\n"
                + render_field_card(candidate.definition_record)
            ),
        },
    )


def validate_candidate_semantic_gate(
    response: CandidateSemanticGateBatch,
    *,
    expected_indices: Sequence[int],
    fixed_definitions: Sequence[FieldDefinition],
) -> dict[int, CandidateSemanticGateDecision]:
    """锁定候选覆盖和固定字段引用，避免模型漏判或指向不存在的固定字段。"""

    expected = set(expected_indices)
    actual = [item.proposal_index for item in response.decisions]
    if len(actual) != len(set(actual)):
        raise ValueError("语义准入中同一 proposal_index 不得出现多次。")
    if set(actual) != expected:
        raise ValueError(
            f"语义准入必须覆盖全部候选。缺少={sorted(expected - set(actual))}；"
            f"额外={sorted(set(actual) - expected)}"
        )
    fixed_ids = {definition.field_id for definition in fixed_definitions}
    decisions: dict[int, CandidateSemanticGateDecision] = {}
    for item in response.decisions:
        if item.status == "covered_by_fixed":
            if item.covered_by_field_id not in fixed_ids:
                raise ValueError(
                    f"候选 {item.proposal_index} 的 covered_by_field_id 不属于固定目录："
                    f"{item.covered_by_field_id!r}。"
                )
        elif item.covered_by_field_id is not None:
            raise ValueError(
                f"候选 {item.proposal_index} 仅在 covered_by_fixed 时可引用固定字段。"
            )
        decisions[item.proposal_index] = item
    return decisions


def recover_candidate_semantic_gate_reference(
    decision: CandidateSemanticGateDecision,
    *,
    fixed_definitions: Sequence[FieldDefinition],
) -> CandidateSemanticGateDecision:
    """从理由中唯一命中的固定字段恢复遗漏引用，不猜测未写明的覆盖目标。"""

    if decision.status != "covered_by_fixed" or decision.covered_by_field_id is not None:
        return decision
    normalized_reason = decision.reason.casefold()
    matches: list[str] = []
    for definition in fixed_definitions:
        terms = (definition.field_id, definition.name, *definition.aliases)
        if any(
            len(term.strip()) >= 2 and term.strip().casefold() in normalized_reason
            for term in terms
        ):
            matches.append(definition.field_id)
    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) != 1:
        return decision
    return decision.model_copy(
        update={"covered_by_field_id": unique_matches[0]}
    )


def validate_candidate_proposal(
    proposal: CandidateProposal,
    *,
    fixed_definitions: Sequence[FieldDefinition],
    source_page_count: int,
) -> CandidateProposalRecord:
    """执行候选结构、新颖性和最小证据门禁；不作开放式业务推断。"""

    if proposal.name.strip() in GENERIC_FIELD_NAMES:
        raise ValueError("字段名称过于宽泛，不能形成稳定元数据。")
    if proposal.field_id in DOCUMENT_INTRINSIC_FIELD_IDS:
        raise ValueError(
            "候选描述的是语言、页数、文件格式或识别质量等文档载体属性，"
            "不是合同明示约定的 Attribute 业务事实；此类信息应由确定性预处理或 Core 元数据管理。"
        )
    is_preliminary_agreement = bool(
        PRELIMINARY_AGREEMENT_IDENTITY_PATTERN.search(proposal.field_id)
    ) or any(term in proposal.name for term in ("初步协议", "预备协议", "签署承诺"))
    if is_preliminary_agreement and not PRELIMINARY_AGREEMENT_EVIDENCE_PATTERN.search(
        proposal.evidence.source_text
    ):
        raise ValueError(
            "‘经协商达成如下协议、共同遵守/恪守’只是当前合同序言，不能证明"
            "正式签署前另有初步协议；证据必须明确出现意向、预签、草签、"
            "框架协议或后续另行签署等预备安排。"
        )
    if proposal.evidence.page_number > source_page_count:
        raise ValueError("候选证据页码超出当前 PDF 页数。")
    if not FIELD_ID_PATTERN.fullmatch(proposal.field_id):
        raise ValueError("field_id 必须使用 lower_snake_case。")
    if any(char.isdigit() for char in proposal.name):
        raise ValueError("字段名称不得混入某份合同的具体编号或数值。")
    compiled_output = compile_output_description(proposal.output)
    _validate_output_definition(compiled_output)
    generalized_extraction_rule = validate_generalized_extraction_rule(
        proposal.extraction_rule
    )

    candidate_terms = {_normalise_term(proposal.name)}
    for definition in fixed_definitions:
        if proposal.field_id == definition.field_id:
            raise ValueError(f"已被固定字段 {definition.field_id} 覆盖。")
        overlap = candidate_terms & _terms(definition)
        if overlap:
            raise ValueError(
                f"候选名称或别名与固定字段 {definition.field_id} 精确重合：{sorted(overlap)}"
            )

    record = {
        "field_id": proposal.field_id,
        "name": proposal.name.strip(),
        "meaning": proposal.meaning.strip(),
        "aliases": [],
        "not_meaning": [],
        "output": compiled_output,
        "extraction_rule": generalized_extraction_rule,
        # 发现阶段不把合同原文作为正式字段 examples 保存。
        "examples": [],
    }
    validate_definition_business_consistency(record)
    try:
        definition = YamlFieldCatalog._to_definition(record, FieldKind.ATTRIBUTE)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"候选字段定义无法通过领域契约：{error}") from error
    return CandidateProposalRecord(
        definition=definition,
        definition_record=record,
        novelty_reason=proposal.novelty_reason.strip(),
        evidence_page_number=proposal.evidence.page_number,
        evidence_text=proposal.evidence.source_text.strip(),
    )


def _candidate_views(record: CandidateProposalRecord) -> dict[str, str]:
    definition = record.definition
    output = record.definition_record["output"]
    return {
        "label": "名称：" + definition.name,
        "meaning": "字段含义：" + definition.meaning,
        "structure": "输出结构：" + _compact_json(output),
    }


def _render_definition(record: CandidateProposalRecord) -> str:
    return render_field_card(record.definition_record)


def build_discovery_prompt_before_images(
    *,
    core_definitions: Sequence[FieldDefinition],
    attribute_definitions: Sequence[FieldDefinition],
    max_candidates: int,
) -> str:
    """构造跨合同稳定的发现前缀，使图像成为第一个合同可变输入。"""

    return render_discovery_prompt(
        "01_propose_fields.txt",
        {
            "{{MAX_CANDIDATES}}": str(max_candidates),
            "{{OUTPUT_DESCRIPTION_RULES}}": OUTPUT_DESCRIPTION_PROMPT_RULES,
            "{{CORE_DEFINITIONS}}": _render_fixed_catalog(
                "固定 Discovery Core（覆盖约束）", core_definitions
            ),
            "{{ATTRIBUTE_DEFINITIONS}}": _render_fixed_catalog(
                "固定 Discovery Attribute（覆盖约束）", attribute_definitions
            ),
        },
    )


def build_discovery_prompt_after_images(
    *, core_status_context: str, attribute_status_context: str, page_visibility_context: str
) -> str:
    """附加每份合同可变信息；置于图像后不破坏跨合同的静态发现前缀。"""

    return render_discovery_prompt(
        "01b_propose_fields_context.txt",
        {
            "{{PAGE_VISIBILITY_CONTEXT}}": page_visibility_context,
            "{{CORE_STATUS_CONTEXT}}": core_status_context,
            "{{ATTRIBUTE_STATUS_CONTEXT}}": attribute_status_context,
        },
    )


def build_relation_prompt(
    *, proposal: CandidateProposalRecord, match: CandidateMatch, pool: "CandidateVectorPool"
) -> RelationPromptParts:
    """构造单对字段的纯文本判别，不重复向模型发送与字段归属无关的合同图像。"""

    identity = pool.identity(match.candidate_id)
    preamble = render_discovery_prompt(
        "06a_compare_relation_preamble.txt",
        {"{{CURRENT_CANDIDATE}}": _render_definition(proposal)},
    )
    target = render_discovery_prompt(
        "06b_compare_relation_target.txt",
        {
            "{{TARGET_CANDIDATE}}": (
                f"候选身份：{identity.candidate_id}\n"
                f"所属分组：{identity.group_id}\n"
                f"融合排名分数：{round(match.fused_score, 8)}\n"
                + render_field_card(identity.proposal.definition_record)
            ),
        },
    )
    return RelationPromptParts(preamble=preamble, target=target)


def _messages(
    *,
    pre_image_prompt: str,
    post_image_prompt: str,
    context: PdfExtractionContext,
    include_images: bool = True,
    system_message: str = SYSTEM_MESSAGE,
) -> list[dict[str, Any]]:
    """允许发现调用使用图像，而定义归属调用保持轻量的纯文本输入。"""

    content: list[dict[str, Any]] = [{"type": "text", "text": pre_image_prompt}]
    if include_images:
        content.extend(
            {"type": "image_url", "image_url": {"url": str(image["data_url"])}}
            for image in context.images
        )
    content.append({"type": "text", "text": post_image_prompt})
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": content},
    ]


async def invoke_structured(
    *,
    client: AsyncOpenAI,
    context: PdfExtractionContext,
    settings: ProjectSettings,
    pre_image_prompt: str,
    post_image_prompt: str,
    schema_model: type[BaseModel],
    schema_name: str,
    max_completion_tokens: int,
    include_images: bool = True,
    system_message: str = SYSTEM_MESSAGE,
) -> tuple[BaseModel, dict[str, Any]]:
    """实验模型调用不落盘 raw response，但保留可定位的时间与 token 指标。"""

    started_at = time.perf_counter()
    schema = schema_model.model_json_schema()
    generation = settings.models.mllm.generation
    async with context.model_request_limiter.slot():
        completion = await client.chat.completions.create(
            model=settings.models.mllm.model,
            messages=_messages(
                pre_image_prompt=pre_image_prompt,
                post_image_prompt=post_image_prompt,
                context=context,
                include_images=include_images,
                system_message=system_message,
            ),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            max_completion_tokens=min(
                generation.max_completion_tokens, max_completion_tokens
            ),
            temperature=generation.temperature,
            top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            extra_body={
                "top_k": generation.top_k,
                "repetition_penalty": generation.repetition_penalty,
                "seed": generation.seed,
            },
        )
    metrics = {
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "finish_reason": completion.choices[0].finish_reason,
        "image_count": len(context.images) if include_images else 0,
    }
    content = completion.choices[0].message.content
    if not content:
        raise StructuredModelResponseError("模型未返回结构化 JSON。", metrics=metrics)
    try:
        parsed = schema_model.model_validate_json(content)
    except Exception as error:
        raise StructuredModelResponseError(
            "模型返回未通过实验 JSON 包络校验。", metrics=metrics
        ) from error
    return parsed, metrics


async def invoke_candidate_proposals(
    *,
    client: AsyncOpenAI,
    context: PdfExtractionContext,
    settings: ProjectSettings,
    pre_image_prompt: str,
    post_image_prompt: str,
    schema_name: str,
    max_completion_tokens: int,
) -> tuple[list[tuple[int, CandidateProposal]], list[CandidateProposalParseFailure], dict[str, Any]]:
    """逐项解析候选批次，避免单个坏候选使同批合法候选全部丢失。

    服务端仍接收完整批次 JSON Schema；本地不再将整个 candidates 数组一次性 Pydantic
    校验。外层 JSON 无法解析或不是合法批次时无法可靠定位候选，仍按整次调用失败处理。
    """

    started_at = time.perf_counter()
    schema = CandidateProposalBatch.model_json_schema()
    generation = settings.models.mllm.generation
    async with context.model_request_limiter.slot():
        completion = await client.chat.completions.create(
            model=settings.models.mllm.model,
            messages=_messages(
                pre_image_prompt=pre_image_prompt,
                post_image_prompt=post_image_prompt,
                context=context,
                include_images=True,
                system_message=SYSTEM_MESSAGE,
            ),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            max_completion_tokens=min(generation.max_completion_tokens, max_completion_tokens),
            temperature=generation.temperature,
            top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            extra_body={
                "top_k": generation.top_k,
                "repetition_penalty": generation.repetition_penalty,
                "seed": generation.seed,
            },
        )
    metrics = {
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "finish_reason": completion.choices[0].finish_reason,
        "image_count": len(context.images),
    }
    content = completion.choices[0].message.content
    if not content:
        raise StructuredModelResponseError("模型未返回结构化 JSON。", metrics=metrics)
    try:
        valid, failures = parse_candidate_proposal_batch_payload(content)
    except ValueError as error:
        raise StructuredModelResponseError(str(error), metrics=metrics) from error
    return valid, failures, metrics


def parse_candidate_proposal_batch_payload(
    content: str,
) -> tuple[list[tuple[int, CandidateProposal]], list[CandidateProposalParseFailure]]:
    """解析候选包络，并将每一项独立送入 CandidateProposal 契约。"""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("模型返回的候选批次不是可解析 JSON。") from error
    if not isinstance(payload, dict) or set(payload) != {"candidates"}:
        raise ValueError("模型返回的候选批次包络不符合要求。")
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 5:
        raise ValueError("模型返回的 candidates 必须是最多五项的数组。")

    valid: list[tuple[int, CandidateProposal]] = []
    failures: list[CandidateProposalParseFailure] = []
    for proposal_index, raw_candidate in enumerate(raw_candidates, start=1):
        try:
            valid.append((proposal_index, CandidateProposal.model_validate(raw_candidate)))
        except Exception as error:
            failures.append(
                CandidateProposalParseFailure(
                    proposal_index=proposal_index,
                    payload=raw_candidate,
                    error=str(error)[:1200],
                )
            )
    return valid, failures


class CandidateVectorPool:
    """只容纳本批次新字段身份的三视角 LlamaIndex 内存候选池。"""

    def __init__(self, embedding_client: Qwen3VLEmbeddingClient) -> None:
        self._embedding_client = embedding_client
        self._stores = {view: SimpleVectorStore() for view in VECTOR_VIEW_WEIGHTS}
        self._identities: dict[str, CandidateIdentity] = {}
        self._relation_edges: set[tuple[str, str, Relation]] = set()
        self._next_candidate_number = 1
        self._next_group_number = 1

    @property
    def size(self) -> int:
        return len(self._identities)

    def identity(self, candidate_id: str) -> CandidateIdentity:
        return self._identities[candidate_id]

    async def top_matches(
        self, proposal: CandidateProposalRecord, *, limit: int
    ) -> list[CandidateMatch]:
        if limit < 1:
            raise ValueError("Top-K 必须大于 0。")
        if not self._identities:
            return []
        views = _candidate_views(proposal)
        embeddings = await asyncio.gather(
            *(self._embedding_client.embed_field_summary(views[view]) for view in views)
        )
        ranks: dict[str, list[tuple[str, int]]] = {}
        raw_scores: dict[str, dict[str, float]] = {}
        for view, embedding in zip(views, embeddings, strict=True):
            result = await self._stores[view].aquery(
                VectorStoreQuery(
                    query_embedding=[float(value) for value in embedding],
                    similarity_top_k=min(limit, self.size),
                )
            )
            ranks[view] = [
                (candidate_id, rank)
                for rank, candidate_id in enumerate(result.ids or [], start=1)
                if candidate_id in self._identities
            ]
            raw_scores[view] = {
                candidate_id: float(score)
                for candidate_id, score in zip(
                    result.ids or [], result.similarities or [], strict=False
                )
                if candidate_id in self._identities
            }
        scores: dict[str, float] = {}
        best_ranks: dict[str, int] = {}
        for view, results in ranks.items():
            for candidate_id, rank in results:
                scores[candidate_id] = scores.get(candidate_id, 0.0) + (
                    VECTOR_VIEW_WEIGHTS[view] / (RRF_OFFSET + rank)
                )
                best_ranks[candidate_id] = min(best_ranks.get(candidate_id, rank), rank)
        ordered = sorted(scores, key=lambda item: (-scores[item], best_ranks[item], item))[:limit]
        return [
            CandidateMatch(
                candidate_id=candidate_id,
                group_id=self._identities[candidate_id].group_id,
                fused_score=scores[candidate_id],
                best_rank=best_ranks[candidate_id],
                view_scores={
                    view: raw_scores.get(view, {}).get(candidate_id, 0.0)
                    for view in views
                },
                view_ranks={
                    view: next(
                        (
                            rank
                            for ranked_candidate_id, rank in ranks.get(view, [])
                            if ranked_candidate_id == candidate_id
                        ),
                        0,
                    )
                    for view in views
                },
            )
            for candidate_id in ordered
        ]

    async def create_identity(
        self,
        proposal: CandidateProposalRecord,
        *,
        document_id: str,
        group_id: str | None = None,
    ) -> CandidateIdentity:
        candidate_id = f"candidate_{self._next_candidate_number:04d}"
        self._next_candidate_number += 1
        resolved_group_id = group_id or f"group_{self._next_group_number:04d}"
        if group_id is None:
            self._next_group_number += 1
        identity = CandidateIdentity(
            candidate_id=candidate_id,
            group_id=resolved_group_id,
            proposal=proposal,
        )
        identity.observe(
            document_id=document_id,
            page_number=proposal.evidence_page_number,
            evidence_hash=proposal.evidence_hash,
        )
        views = _candidate_views(proposal)
        embeddings = await asyncio.gather(
            *(self._embedding_client.embed_field_summary(views[view]) for view in views)
        )
        for view, embedding in zip(views, embeddings, strict=True):
            await self._stores[view].async_add(
                [
                    TextNode(
                        id_=candidate_id,
                        text=views[view],
                        embedding=[float(value) for value in embedding],
                    )
                ]
            )
        self._identities[candidate_id] = identity
        return identity

    def reuse_identity(
        self, candidate_id: str, *, document_id: str, proposal: CandidateProposalRecord
    ) -> CandidateIdentity:
        identity = self.identity(candidate_id)
        identity.observe(
            document_id=document_id,
            page_number=proposal.evidence_page_number,
            evidence_hash=proposal.evidence_hash,
        )
        return identity

    def connect_governance_relations(
        self,
        source_candidate_id: str,
        targets: Sequence[tuple[str, Relation]],
    ) -> str:
        """用关系图连通治理语义族，并将连通分量统一到稳定的最小 group_id。

        group_id 只表示“应放在一起治理”，不声明分量内任意两点都同义。关系的具体类型保留在
        边记录中，后续组级模型仍需决定合并、拆分或淘汰。
        """

        source = self.identity(source_candidate_id)
        connected_candidate_ids = {source_candidate_id}
        for target_candidate_id, relation in targets:
            if relation == "unrelated" or target_candidate_id == source_candidate_id:
                continue
            self.identity(target_candidate_id)
            left, right = sorted((source_candidate_id, target_candidate_id))
            self._relation_edges.add((left, right, relation))
            connected_candidate_ids.add(target_candidate_id)
        if len(connected_candidate_ids) == 1:
            return source.group_id

        group_ids = {
            self._identities[candidate_id].group_id
            for candidate_id in connected_candidate_ids
        }
        # 目标可能分别属于既有连通分量；把所有同组成员一并纳入本次 union。
        component_ids = {
            candidate_id
            for candidate_id, identity in self._identities.items()
            if identity.group_id in group_ids
        }
        canonical_group_id = min(group_ids)
        for candidate_id in component_ids:
            self._identities[candidate_id].group_id = canonical_group_id
        return canonical_group_id

    def report(self) -> list[dict[str, Any]]:
        return [self._identities[key].report() for key in sorted(self._identities)]

    def relation_graph_report(self) -> dict[str, Any]:
        components: dict[str, list[str]] = {}
        for candidate_id, identity in sorted(self._identities.items()):
            components.setdefault(identity.group_id, []).append(candidate_id)
        return {
            "edges": [
                {
                    "source_candidate_id": source,
                    "target_candidate_id": target,
                    "relation": relation,
                }
                for source, target, relation in sorted(self._relation_edges)
            ],
            "components": [
                {"group_id": group_id, "candidate_ids": candidate_ids}
                for group_id, candidate_ids in sorted(components.items())
            ],
        }


def validate_single_relation_semantics(
    *,
    proposal: CandidateProposalRecord,
    target: CandidateProposalRecord,
    judgement: SingleRelationJudgement,
) -> SingleRelationJudgement:
    """拦截最危险的顶层/子字段错配，并规范关系理由的固定结尾。"""

    source_type = proposal.definition.output.type
    target_type = target.definition.output.type
    exact_generated_identity = (
        proposal.definition.field_id == target.definition.field_id
        and _normalise_term(proposal.definition.name)
        == _normalise_term(target.definition.name)
    )
    if exact_generated_identity and judgement.relation != "same":
        raise ValueError(
            "两个候选具有相同 field_id 和规范名称；输出表示差异不能把同一身份判为"
            f" {judgement.relation}。请按完整业务事实重新判断。"
        )
    if judgement.relation == "same" and ((source_type == "object") != (target_type == "object")):
        raise ValueError(
            "same 必须是顶层字段完整一一对应；当前只有一侧为 object，"
            "疑似把标量字段误配到宽泛对象的某个子字段。请重新比较完整字段边界。"
        )
    reason = finalize_relation_reason(reason=judgement.reason, relation=judgement.relation)
    if judgement.relation == "unrelated":
        mentions_type_difference = bool(
            re.search(r"(?:output|输出|类型).{0,24}(?:不同|不一致|差异)", reason, re.I)
        )
        has_business_boundary = any(
            marker in reason
            for marker in ("业务", "事实", "含义", "边界", "职责", "义务", "触发", "对象")
        )
        if mentions_type_difference and not has_business_boundary:
            raise ValueError(
                "output 类型差异不能单独支持 unrelated；请说明两个字段所记录业务事实的边界。"
            )
    return judgement.model_copy(update={"reason": reason})


def validate_relation_judgement(
    judgement: RelationJudgement, matches: Sequence[CandidateMatch]
) -> dict[str, RelationComparison]:
    expected = {match.candidate_id for match in matches}
    actual = [item.target_candidate_id for item in judgement.comparisons]
    if len(actual) != len(set(actual)):
        raise ValueError("同一 Top 候选不得出现多次关系判断。")
    if set(actual) != expected:
        raise ValueError(
            f"关系判断必须覆盖全部 Top 候选。缺少={sorted(expected - set(actual))}；"
            f"额外={sorted(set(actual) - expected)}"
        )
    normalized_items = [
        item.model_copy(
            update={"reason": finalize_relation_reason(reason=item.reason, relation=item.relation)}
        )
        for item in judgement.comparisons
    ]
    return {item.target_candidate_id: item for item in normalized_items}


async def resolve_candidate_identity(
    *,
    proposal: CandidateProposalRecord,
    document_id: str,
    matches: Sequence[CandidateMatch],
    comparisons: dict[str, RelationComparison],
    pool: CandidateVectorPool,
) -> dict[str, Any]:
    """执行身份优先级，并用全部非 unrelated 边维护治理连通分量。"""

    same = [match for match in matches if comparisons[match.candidate_id].relation == "same"]
    if same:
        target = max(same, key=lambda item: (item.fused_score, -item.best_rank, item.candidate_id))
        identity = pool.reuse_identity(
            target.candidate_id, document_id=document_id, proposal=proposal
        )
        graph_targets = [
            (match.candidate_id, comparisons[match.candidate_id].relation)
            for match in matches
            if comparisons[match.candidate_id].relation != "unrelated"
        ]
        group_id = pool.connect_governance_relations(identity.candidate_id, graph_targets)
        return {
            "action": "reuse_identity",
            "candidate_id": identity.candidate_id,
            "group_id": group_id,
            "selected_target_candidate_id": target.candidate_id,
            "related_target_candidate_ids": sorted(
                candidate_id
                for candidate_id, relation in graph_targets
                if relation == "related_distinct"
            ),
        }

    related = [
        match
        for match in matches
        if comparisons[match.candidate_id].relation == "related_distinct"
    ]
    if related:
        target = max(
            related,
            key=lambda item: (item.fused_score, -item.best_rank, item.candidate_id),
        )
        identity = await pool.create_identity(
            proposal,
            document_id=document_id,
        )
        group_id = pool.connect_governance_relations(
            identity.candidate_id,
            [(match.candidate_id, "related_distinct") for match in related],
        )
        return {
            "action": "create_identity_in_existing_group",
            "candidate_id": identity.candidate_id,
            "group_id": group_id,
            "selected_target_candidate_id": target.candidate_id,
            "related_target_candidate_ids": sorted(
                match.candidate_id for match in related
            ),
        }

    identity = await pool.create_identity(proposal, document_id=document_id)
    return {
        "action": "create_identity_and_group",
        "candidate_id": identity.candidate_id,
        "group_id": identity.group_id,
        "selected_target_candidate_id": None,
        "related_target_candidate_ids": [],
    }
