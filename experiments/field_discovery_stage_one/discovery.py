"""字段发现第一阶段的实验性候选生成、归并与内存分组实现。"""

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
from experiments.field_discovery_stage_one.field_description import (
    OUTPUT_DESCRIPTION_PROMPT_RULES,
    OutputDescription,
    compile_output_description,
    field_definition_record,
    render_field_card,
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
VECTOR_VIEW_WEIGHTS = {"label": 0.30, "meaning": 0.50, "structure": 0.20}
RRF_OFFSET = 60
FIELD_RELATION_SYSTEM_MESSAGE = (
    "你必须严格依据两个字段定义的语义边界、排除语义和输出结构，输出当前 JSON Schema。"
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
    return "\n\n".join(
        (
            "【任务】\n只修订当前候选字段及其 output 子字段的 extraction_rule。"
            "不得修改字段身份、含义、输出结构或证据。",
            "【通用规则契约】\n"
            "- extraction_rule 是跨合同字段规范，不是本合同证据定位。\n"
            "- 说明什么明确合同事实可以确认该字段、哪些相似事实必须排除、如何规范化输出，"
            "以及缺失或多候选冲突时如何处理。\n"
            "- 禁止页码、条款号、章节序号、固定章节标题、版式位置、当前合同的公司名称、"
            "具体字段值或原句。\n"
            "- 使用与当前字段业务含义一致的语义条件，例如‘与当前字段所描述的合同事项直接关联’；"
            "不能复制其他业务领域的示例词，也不能使用‘从条款7提取’一类位置锚点。",
            "【程序校验失败】\n" + validation_error,
            "【当前字段】\n" + _compact_json(field_context),
            "【输出】\n只输出 extraction_rule 和完整 output。output 除各层 extraction_rule 外必须与"
            "输入逐项一致；没有子字段规则也必须原样返回完整 output。",
        )
    )


def build_candidate_proposal_repair_prompt(
    *, proposal_payload: object, validation_error: str
) -> str:
    """构造单候选结构修复任务；不重读 PDF，也不要求重新发现其他候选。"""

    return "\n\n".join(
        (
            "【任务】\n修复一个未通过候选字段结构契约的候选。只输出这一个候选对象，"
            "不要输出 candidates 包络、其他候选、解释文字或合同字段值。",
            "【修复边界】\n"
            "- 保持当前候选所表达的同一业务事实及已有证据，不得借修复改造成无关的新字段。\n"
            "- 根据程序校验错误修复 field_id、字段描述、output 或 extraction_rule 的格式与结构。\n"
            "- output 是类型描述而不是 JSON Schema；不得输出 nullable、required、"
            "additional_properties 或 anyOf。\n"
            "- evidence 必须仍指向当前合同中已有的明确证据；不得编造。\n"
            + OUTPUT_DESCRIPTION_PROMPT_RULES,
            "【程序校验失败】\n" + validation_error,
            "【待修复候选】\n" + _compact_json(proposal_payload),
            "【输出】\n只输出一个符合 CandidateProposal Schema 的 JSON 对象。"
            "novelty_reason 必须在 status 前，且 status 固定为 accepted。",
        )
    )


def build_candidate_semantic_gate_prompt(
    *,
    candidates: Sequence[tuple[int, CandidateProposalRecord]],
    fixed_definitions: Sequence[FieldDefinition],
) -> str:
    """批量检查固定覆盖、字段原子性和规则一致性，不再次发送合同图像。"""

    candidate_cards = []
    for proposal_index, candidate in candidates:
        candidate_cards.append(
            f"候选序号：{proposal_index}\n" + render_field_card(candidate.definition_record)
        )
    fixed_catalog = _render_fixed_catalog("固定 Core/Attribute（完整覆盖空间）", fixed_definitions)
    return "\n\n".join(
        (
            "【任务】\n对同一份合同刚生成的候选字段执行语义准入。只判断字段定义，"
            "不得重新提取合同内容，也不得改写候选。",
            "【四种状态】\n"
            "- accepted：一个稳定、可跨合同复用的原子业务事实，未被固定字段覆盖，规则与含义一致。\n"
            "- covered_by_fixed：字段整体或其实质语义已经被某个固定字段（包括对象子字段）覆盖；"
            "此时 covered_by_field_id 必须填写对应固定 field_id。\n"
            "- non_atomic：候选把多个可独立缺失、独立检索或独立治理的业务事实装进一个宽泛容器；"
            "object 并非天然不原子，但包装、运输保护、质保期限等独立事项不能因同处一段而合并。\n"
            "- invalid_rule：extraction_rule 或子字段规则与字段 name/meaning 不一致，混入其他业务领域，"
            "或仍依赖页码、条款号、章节标题、版式位置及当前合同专属表达。",
            "【判定要求】\n"
            "- 优先按业务事实判断覆盖关系，不能只比较 field_id、名称或 output 类型。\n"
            "- 固定 object 的某个子字段足以覆盖候选时，也应判 covered_by_fixed。\n"
            "- 同一事实的原文版、结构化版、枚举版或查询切片不构成新字段。\n"
            "- 每个候选序号必须且只能输出一次。reason 最后必须写明‘因此 status=<status>’。\n"
            "- 非 covered_by_fixed 状态的 covered_by_field_id 必须为 null。",
            fixed_catalog,
            "【待审候选】\n" + "\n\n".join(candidate_cards),
            "【输出】\n只输出 JSON，完整覆盖全部候选序号。reason 在 status 之前。",
        )
    )


def build_single_candidate_semantic_gate_prompt(
    *, candidate_index: int, candidate: CandidateProposalRecord, fixed_definitions: Sequence[FieldDefinition]
) -> str:
    """构造单候选语义准入 Prompt，避免单项输出错误阻塞同合同其他候选。"""

    fixed_catalog = _render_fixed_catalog("固定 Core/Attribute（完整覆盖空间）", fixed_definitions)
    return "\n\n".join(
        (
            "【任务】\n对一个刚生成的候选字段执行语义准入。只判断字段定义，"
            "不得重新提取合同内容，也不得改写候选。",
            "【四种状态】\n"
            "- accepted：一个稳定、可跨合同复用的原子业务事实，未被固定字段覆盖，规则与含义一致。\n"
            "- covered_by_fixed：字段整体或其实质语义已经被某个固定字段（包括对象子字段）覆盖；"
            "此时 covered_by_field_id 必须填写对应固定 field_id。\n"
            "- non_atomic：候选把多个可独立缺失、独立检索或独立治理的业务事实装进一个宽泛容器。\n"
            "- invalid_rule：extraction_rule 或子字段规则与字段 name/meaning 不一致，混入其他业务领域，"
            "或仍依赖页码、条款号、章节标题、版式位置及当前合同专属表达。",
            "【判定要求】\n"
            "- 优先按业务事实判断覆盖关系，不能只比较 field_id、名称或 output 类型。\n"
            "- 固定 object 的某个子字段足以覆盖候选时，也应判 covered_by_fixed。\n"
            "- 同一事实的原文版、结构化版、枚举版或查询切片不构成新字段。\n"
            f"- proposal_index 必须固定输出为 {candidate_index}；reason 最后必须写明‘因此 status=<status>’。\n"
            "- 非 covered_by_fixed 状态的 covered_by_field_id 必须为 null。",
            fixed_catalog,
            "【待审候选】\n"
            + f"候选序号：{candidate_index}\n"
            + render_field_card(candidate.definition_record),
            "【输出】\n只输出一个候选语义准入 JSON 对象。reason 在 status 之前。",
        )
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


def validate_candidate_proposal(
    proposal: CandidateProposal,
    *,
    fixed_definitions: Sequence[FieldDefinition],
    source_page_count: int,
) -> CandidateProposalRecord:
    """执行候选结构、新颖性和最小证据门禁；不作开放式业务推断。"""

    if proposal.name.strip() in GENERIC_FIELD_NAMES:
        raise ValueError("字段名称过于宽泛，不能形成稳定元数据。")
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

    return "\n\n".join(
        [
            "【任务】\n"
            "你正在进行合同字段发现。直接阅读全部 PDF 页面，只提出当前合同中有明确证据、"
            "且未被固定字段覆盖的潜在 Attribute 字段。最多提出 "
            f"{max_candidates} 个；不足时宁可少提或返回空数组，不得凑数。",
            "【候选准入】\n"
            "- 字段必须具有稳定业务含义、可跨合同复用，并具有检索、比较或审核价值。\n"
            "- 一个候选只能表达一个可独立缺失、检索和治理的业务事实。object 只用于同一事实"
            "不可分割的组成部分，不得把同一条款中的多个事项打包成宽泛容器。\n"
            "- 不得提出合同名称、合同编号、主体、金额、产品名称等已经由固定字段覆盖的同义概念。\n"
            "- 不得把任意技术细节、联系人、银行账号、单份合同编号或“其他信息”作为字段。\n"
            "- field_id 使用 lower_snake_case；第一阶段不生成 aliases、not_meaning 或 examples。\n"
            + OUTPUT_DESCRIPTION_PROMPT_RULES
            + "\n"
            "- evidence.page_number 与 evidence.source_text 必须直接来自 PDF；source_text 仅作本次结构核验。\n"
            "- evidence 保存当前合同的页码和原文位置；extraction_rule 是字段库级跨合同规则，"
            "两者不得混用。\n"
            "- 字段及 output 子字段的 extraction_rule 必须说明确认条件、排除边界、规范化方式及"
            "缺失/冲突处理；禁止写入"
            "页码、条款号、章节序号、固定章节标题、版式位置、当前合同公司名称、具体字段值或原句。\n"
            "- 规则只能使用与当前字段含义一致的语义条件；不得照搬其他领域示例词，也不得使用"
            "‘从条款7/其他约定中提取’等位置锚点。\n"
            "- novelty_reason 必须解释为什么它没有被固定 Core/Attribute 覆盖；可写充分理由，最长 1200 字符。\n"
            "- 每个候选对象最后两个键必须依次为 novelty_reason、status。先给出 novelty_reason，随后"
            "固定输出 status=accepted；语义为“novelty_reason=……，因此 status=accepted”。"
            "status 只是你的提议，程序仍会执行独立门禁并可能拒绝。",
            _render_fixed_catalog("固定 Discovery Core（覆盖约束）", core_definitions),
            _render_fixed_catalog("固定 Discovery Attribute（覆盖约束）", attribute_definitions),
            "【输出】\n只输出 JSON。candidates 可以为空；不得输出固定字段、合同字段值汇总、"
            "条款摘要或未要求的键。对象键严格按 Schema 顺序输出。",
        ]
    )


def build_discovery_prompt_after_images(
    *, core_status_context: str, attribute_status_context: str, page_visibility_context: str
) -> str:
    """附加每份合同可变信息；置于图像后不破坏跨合同的静态发现前缀。"""

    return "\n\n".join(
        [
            page_visibility_context,
            core_status_context,
            attribute_status_context,
            "【执行】\n现在直接阅读以上全部 PDF 页面，并依据图像前的字段发现任务、固定覆盖约束"
            "与本合同提取状态，输出候选 JSON。",
        ]
    )


def build_relation_prompt(
    *, proposal: CandidateProposalRecord, match: CandidateMatch, pool: "CandidateVectorPool"
) -> RelationPromptParts:
    """构造单对字段的纯文本判别，不重复向模型发送与字段归属无关的合同图像。"""

    identity = pool.identity(match.candidate_id)
    preamble = "\n\n".join(
        [
            "【任务】\n判断当前新字段与一个候选字段的归属关系。每个 Top 候选都会单独判别；"
            "你只负责当前这一对，程序会收集完整 Top 5 后再作最终身份或分组决定。",
            "【关系定义】\n"
            "- same：两个顶层字段完整地一一对应同一个业务事实，名称、边界和输出语义实质一致。"
            "仅与 object 的某一个子字段相同，绝不能判 same。\n"
            "- related_distinct：存在业务或语义关联，但必须保留为不同字段。\n"
            "- unrelated：没有需要保留的字段关系。\n"
            "meaning、extraction_rule 和 output 共同描述边界；词面相近或都包含“期限”等词不代表 same。"
            "output 类型不同只能触发进一步比较，不能单独作为 unrelated 的理由；同一事实的原文版、"
            "结构化版或枚举版仍可能是 same。",
            "【当前新字段】\n" + _render_definition(proposal),
        ]
    )
    target = "\n\n".join(
        [
            "【待比较候选】\n"
            + (
                f"候选身份：{identity.candidate_id}\n"
                f"所属分组：{identity.group_id}\n"
                f"融合排名分数：{round(match.fused_score, 8)}\n"
                + render_field_card(identity.proposal.definition_record)
            ),
            "【输出】\n只输出当前字段对的 JSON。最后两个键必须依次为 reason、relation："
            "reason 先写具体边界依据（最长 1200 字符），其最后必须精确以“因此 relation=<本对象的 relation 值>”"
            "收尾，末尾不得再加标点或文字；随后在 relation 写入同一个选择。"
            "例如 relation 为 unrelated 时，reason 必须以“因此 relation=unrelated”收尾。"
            "对象键严格按 Schema 顺序输出。",
        ]
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
