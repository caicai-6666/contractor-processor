"""字段发现统一流水线与历史复现入口共享的组级字段收敛契约。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contract_processor.application.schemas.core_extraction import build_field_extraction_schema
from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import FieldDefinition
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
from experiments.field_discovery_stage_one.discovery import (
    FIELD_ID_PATTERN,
    GENERIC_FIELD_NAMES,
    _validate_output_definition,
    validate_generalized_extraction_rule,
)
from experiments.field_discovery_stage_one.field_description import (
    OUTPUT_DESCRIPTION_PROMPT_RULES,
    OutputDescription,
    compile_output_description,
    field_definition_record,
    render_field_card,
)


GroupDecision = Literal["refine_group"]
DISPOSITION = Literal["discarded"]
GROUP_REASON_SUFFIX_PATTERN = re.compile(
    r"(?:[。！？.!]\s*)?因此\s*decision\s*=\s*"
    r"(?P<decision>[a-z_]+)\s*[。！？.!]?\s*$",
    flags=re.DOTALL,
)
DISCARD_REASON_SUFFIX_PATTERN = re.compile(
    r"(?:[。！？.!]\s*)?因此\s*disposition\s*=\s*"
    r"(?P<disposition>[a-z_]+)\s*[。！？.!]?\s*$",
    flags=re.DOTALL,
)


class StrictModel(BaseModel):
    """拒绝额外模型输出键，避免把自由文本误解为字段治理动作。"""

    model_config = ConfigDict(extra="forbid")


class DiscardedCandidate(StrictModel):
    candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=8, max_length=1200)
    disposition: DISPOSITION


class OwnershipFieldPlan(StrictModel):
    """第一段模型只规划候选唯一去向，不承担复杂 output 生成。"""

    plan_id: str = Field(pattern=r"^field_plan_[0-9]{2}$")
    source_candidate_ids: list[str] = Field(min_length=1, max_length=20)
    name: str = Field(min_length=2, max_length=80)
    meaning: str = Field(min_length=10, max_length=500)
    boundary: str = Field(min_length=10, max_length=600)


class GroupOwnershipPlan(StrictModel):
    reason: str = Field(min_length=10, max_length=1600)
    decision: GroupDecision
    final_field_plans: list[OwnershipFieldPlan] = Field(default_factory=list, max_length=20)
    discarded_candidates: list[DiscardedCandidate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_disposition(self) -> "GroupOwnershipPlan":
        if not self.final_field_plans and not self.discarded_candidates:
            raise ValueError(
                "final_field_plans 与 discarded_candidates 不得同时为空。"
            )
        return self


class FinalFieldDefinitionSuggestion(StrictModel):
    """第二段模型只定义一个已由程序绑定来源的最终字段。"""

    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=2, max_length=80)
    meaning: str = Field(min_length=10, max_length=500)
    output: OutputDescription
    extraction_rule: str = Field(min_length=10, max_length=800)


GlobalSemanticStatus = Literal[
    "accepted",
    "covered_by_fixed",
    "duplicate_final",
    "overlap_review",
]


class GlobalSemanticDecision(StrictModel):
    final_field_ref: str = Field(min_length=3, max_length=160)
    target_ref: str | None = Field(default=None, max_length=160)
    reason: str = Field(min_length=10, max_length=1200)
    status: GlobalSemanticStatus


class SingleGlobalSemanticDecision(StrictModel):
    """单个最终字段的门禁响应；当前字段引用由程序绑定。"""

    target_ref: str | None = Field(default=None, max_length=160)
    reason: str = Field(min_length=10, max_length=1200)
    status: GlobalSemanticStatus


class GlobalConflictConfirmation(StrictModel):
    reason: str = Field(min_length=10, max_length=1200)
    status: Literal["confirmed_conflict", "false_positive"]


class GlobalSemanticGateResponse(StrictModel):
    decisions: list[GlobalSemanticDecision] = Field(default_factory=list, max_length=100)


@dataclass(frozen=True, slots=True)
class FieldIdentity:
    candidate_id: str
    group_id: str
    definition: dict[str, Any]
    occurrence_count: int
    contract_count: int

    def model_record(self) -> dict[str, Any]:
        """仅暴露字段定义和统计，不泄露合同原文或具体字段值。"""

        record = {
            "candidate_id": self.candidate_id,
            "field_id": self.definition["field_id"],
            "name": self.definition["name"],
            "meaning": self.definition["meaning"],
            "output": self.definition["output"],
            "extraction_rule": self.definition["extraction_rule"],
            "statistics": {
                "occurrence_count": self.occurrence_count,
                "contract_count": self.contract_count,
            },
        }
        for name in ("aliases", "not_meaning"):
            values = self.definition.get(name)
            if isinstance(values, list) and values:
                record[name] = values
        return record

    def prompt_card(self) -> str:
        return "\n".join(
            (
                f"候选身份：{self.candidate_id}",
                f"统计：出现 {self.occurrence_count} 次，涉及 {self.contract_count} 份合同",
                render_field_card(self.definition),
            )
        )


@dataclass(frozen=True, slots=True)
class GroupProfile:
    group_id: str
    members: tuple[FieldIdentity, ...]

    def member(self, candidate_id: str) -> FieldIdentity:
        for member in self.members:
            if member.candidate_id == candidate_id:
                return member
        raise KeyError(f"分组 {self.group_id} 不包含候选 {candidate_id}。")

    def model_record(self, *, max_members: int) -> dict[str, Any]:
        if max_members < 1:
            raise ValueError("每组展示成员数必须至少为 1。")
        if len(self.members) > max_members:
            raise ValueError(
                f"分组 {self.group_id} 有 {len(self.members)} 个候选，超过单次治理上限 "
                f"{max_members}；请显式提高上限或先设计分组拆分策略。"
            )
        selected = sorted(self.members, key=lambda item: item.candidate_id)
        return {
            "group_id": self.group_id,
            "member_count": len(self.members),
            "members": [member.model_record() for member in selected],
        }

    def prompt_text(self, *, max_members: int) -> str:
        self.model_record(max_members=max_members)
        return "\n\n".join(
            (
                f"分组：{self.group_id}",
                f"成员数：{len(self.members)}",
                *(
                    member.prompt_card()
                    for member in sorted(
                        self.members, key=lambda item: item.candidate_id
                    )
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class ValidatedFinalField:
    definition: FieldDefinition
    definition_record: dict[str, Any]
    source_candidate_ids: tuple[str, ...]
    extraction_schema: dict[str, Any]

    def report(self) -> dict[str, Any]:
        return {
            "source_candidate_ids": list(self.source_candidate_ids),
            "definition": self.definition_record,
            "extraction_json_schema": self.extraction_schema,
        }


def load_group_profiles(payload: Any) -> tuple[GroupProfile, ...]:
    """从冻结的第一阶段候选池构造只读分组视图。"""

    if not isinstance(payload, list):
        raise ValueError("candidate_pool.json 必须是数组。")
    if not payload:
        return ()
    grouped: dict[str, list[FieldIdentity]] = {}
    seen_candidate_ids: set[str] = set()
    required_definition_keys = {"field_id", "name", "meaning", "output", "extraction_rule"}
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"候选池第 {index} 项不是对象。")
        candidate_id = item.get("candidate_id")
        group_id = item.get("group_id")
        definition = item.get("suggested_definition")
        statistics = item.get("statistics")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError(f"候选池第 {index} 项缺少 candidate_id。")
        if candidate_id in seen_candidate_ids:
            raise ValueError(f"候选池存在重复 candidate_id：{candidate_id}。")
        seen_candidate_ids.add(candidate_id)
        if not isinstance(group_id, str) or not group_id:
            raise ValueError(f"候选 {candidate_id} 缺少 group_id。")
        if not isinstance(definition, dict) or required_definition_keys - set(definition):
            raise ValueError(f"候选 {candidate_id} 的 suggested_definition 不完整。")
        if not isinstance(statistics, dict):
            raise ValueError(f"候选 {candidate_id} 缺少 statistics。")
        occurrence_count = statistics.get("occurrence_count")
        contract_count = statistics.get("contract_count")
        if not isinstance(occurrence_count, int) or occurrence_count < 1:
            raise ValueError(f"候选 {candidate_id} 的 occurrence_count 非法。")
        if not isinstance(contract_count, int) or contract_count < 1:
            raise ValueError(f"候选 {candidate_id} 的 contract_count 非法。")
        grouped.setdefault(group_id, []).append(
            FieldIdentity(
                candidate_id=candidate_id,
                group_id=group_id,
                definition=definition,
                occurrence_count=occurrence_count,
                contract_count=contract_count,
            )
        )
    return tuple(
        GroupProfile(
            group_id=group_id,
            members=tuple(sorted(members, key=lambda item: item.candidate_id)),
        )
        for group_id, members in sorted(grouped.items())
    )


def build_group_ownership_prompt(
    *, profile: GroupProfile, max_members_per_group: int
) -> str:
    """第一段只规划候选唯一去向，避免在复杂字段生成中重复分配 candidate_id。"""

    if len(profile.members) < 2:
        raise ValueError("单候选组无需模型归并，应走确定性直通分支。")
    required_ids = "、".join(member.candidate_id for member in profile.members)
    return "\n\n".join(
        (
            "【任务】\n对一个关系图连通的候选组规划最终字段身份。连通只表示这些字段应一起治理，"
            "不表示所有候选同义。当前步骤只决定合并、分拆或淘汰，不生成 output。",
            "【唯一去向规则】\n"
            "- 每个输入 candidate_id 必须且只能出现一次：属于一个 final_field_plan，或进入 discarded_candidates。\n"
            "- final_field_plans 与 discarded_candidates 不得同时为空；不确定是否保留时也不得省略，"
            "应先建立独立 plan，只有明确低价值时才 discarded。\n"
            "- 同一顶层业务事实的原文版、结构化版、枚举版或查询切片应进入同一个 plan。\n"
            "- 语义相关但可独立缺失、检索或治理的事实应进入不同 plan。\n"
            "- 只与宽泛 object 的某个子字段相同，不代表应继承整个 object 身份。\n"
            "- 低价值、一次性编号、过宽泛容器或无法形成稳定元数据的候选应淘汰。\n"
            "- plan_id 从 field_plan_01 开始连续编号；name 必须是可读的中文业务名称，不得填写 field_id；"
            "meaning 描述最终字段，boundary 说明与同组其他事实的边界。",
            "【必须逐一处置的候选 ID】\n" + required_ids,
            "【输入候选组】\n" + profile.prompt_text(max_members=max_members_per_group),
            "【输出】\n只输出 JSON。reason 先说明规划依据，最后精确以"
            "‘因此 decision=refine_group’收尾；随后 decision=refine_group。",
        )
    )


def validate_group_ownership_plan(
    *, response: GroupOwnershipPlan, profile: GroupProfile
) -> dict[str, Any]:
    expected = {member.candidate_id for member in profile.members}
    plan_ids = [plan.plan_id for plan in response.final_field_plans]
    if len(plan_ids) != len(set(plan_ids)):
        raise ValueError("final_field_plans 的 plan_id 不得重复。")
    expected_plan_ids = [f"field_plan_{index:02d}" for index in range(1, len(plan_ids) + 1)]
    if plan_ids != expected_plan_ids:
        raise ValueError(f"plan_id 必须连续且按顺序输出，期望={expected_plan_ids}。")
    for plan in response.final_field_plans:
        if FIELD_ID_PATTERN.fullmatch(plan.name) or not re.search(r"[\u4e00-\u9fff]", plan.name):
            raise ValueError(
                f"{plan.plan_id}.name 必须是可读的中文业务字段名称，不能使用 field_id："
                f"{plan.name!r}。"
            )
    assigned = [
        candidate_id
        for plan in response.final_field_plans
        for candidate_id in plan.source_candidate_ids
    ]
    discarded = [item.candidate_id for item in response.discarded_candidates]
    observed = assigned + discarded
    duplicates = sorted(
        candidate_id
        for candidate_id in set(observed)
        if observed.count(candidate_id) > 1
    )
    if duplicates:
        raise ValueError(f"每个候选只能有一个去向，重复 candidate_id：{duplicates}。")
    missing = sorted(expected - set(observed))
    unknown = sorted(set(observed) - expected)
    if missing or unknown:
        raise ValueError(f"候选覆盖必须精确匹配当前组。缺少={missing}；额外={unknown}。")
    plan_by_candidate = {
        candidate_id: plan.plan_id
        for plan in response.final_field_plans
        for candidate_id in plan.source_candidate_ids
    }
    # 同 field_id 或同规范名称只代表同一事实的不同表达，不能被模型拆成“原文版/结构化版”。
    for index, left in enumerate(profile.members):
        for right in profile.members[index + 1 :]:
            exact_identity = (
                left.definition["field_id"] == right.definition["field_id"]
                or _normalize_term(str(left.definition["name"]))
                == _normalize_term(str(right.definition["name"]))
            )
            left_plan = plan_by_candidate.get(left.candidate_id)
            right_plan = plan_by_candidate.get(right.candidate_id)
            if exact_identity and left_plan and right_plan and left_plan != right_plan:
                raise ValueError(
                    "同一字段身份的表示差异不得拆成多个最终字段："
                    f"{left.candidate_id}/{right.candidate_id} 分别进入 "
                    f"{left_plan}/{right_plan}。请合并到同一 plan。"
                )
    return {
        "group_id": profile.group_id,
        "reason": finalize_group_reason(reason=response.reason, decision=response.decision),
        "decision": response.decision,
        "final_field_plans": [plan.model_dump(mode="json") for plan in response.final_field_plans],
        "discarded_candidates": [
            {
                "candidate_id": item.candidate_id,
                "reason": finalize_discard_reason(reason=item.reason, disposition=item.disposition),
                "disposition": item.disposition,
            }
            for item in response.discarded_candidates
        ],
        "input_candidate_ids": sorted(expected),
    }


def build_final_field_definition_prompt(
    *, profile: GroupProfile, plan: OwnershipFieldPlan, sibling_plans: Sequence[OwnershipFieldPlan]
) -> str:
    selected = [profile.member(candidate_id) for candidate_id in plan.source_candidate_ids]
    siblings = [item for item in sibling_plans if item.plan_id != plan.plan_id]
    sibling_text = (
        "\n".join(f"- {item.name}：{item.meaning}；边界={item.boundary}" for item in siblings)
        if siblings
        else "- 无。"
    )
    return "\n\n".join(
        (
            "【任务】\n为程序已经确定来源的一个最终字段生成正式字段描述。不得重新分配、增加或删除候选。",
            "【字段身份计划】\n"
            f"plan_id：{plan.plan_id}\n名称建议：{plan.name}\n含义建议：{plan.meaning}\n边界：{plan.boundary}",
            "【绑定来源候选】\n" + "\n\n".join(member.prompt_card() for member in selected),
            "【同组其他最终字段】\n" + sibling_text,
            "【定义规则】\n"
            "- field_id 使用稳定 lower_snake_case；name/meaning 必须与身份计划一致。\n"
            "- 本步骤不生成 aliases、not_meaning 或 examples：程序从来源候选名称/既有边界和兄弟"
            "字段确定性补齐，避免编造。\n"
            "- examples 本阶段不生成。output 只描述 type 及该类型不可缺少的参数，不得手写 JSON Schema。\n"
            + OUTPUT_DESCRIPTION_PROMPT_RULES
            + "\n"
            "- extraction_rule 只描述跨合同适用的确认条件、排除边界、规范化及缺失/冲突处理。"
            "禁止页码、条款号、章节标题、版式位置、当前合同实体、具体值或原句；"
            "规则中的业务对象必须与本字段 name/meaning 一致。\n"
            "- 正则约束必须写入 output.pattern，禁止写成 format='pattern: ...'。",
            "【输出】\n只输出当前一个字段的 JSON，不得输出 source_candidate_ids、plan_id、aliases、"
            "not_meaning 或 examples。",
        )
    )


def _normalize_term(value: str) -> str:
    return re.sub(r"[\s_\-()（）【】\[\]{}]", "", value).casefold()


def _unique_strings(values: Sequence[str], *, exclude: Sequence[str] = ()) -> list[str]:
    excluded = {_normalize_term(value) for value in exclude}
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized_value = value.strip()
        key = _normalize_term(normalized_value)
        if not normalized_value or not key or key in excluded or key in seen:
            continue
        seen.add(key)
        result.append(normalized_value)
    return result


def validate_final_field_definition(
    *,
    suggestion: FinalFieldDefinitionSuggestion,
    plan: OwnershipFieldPlan,
    sibling_plans: Sequence[OwnershipFieldPlan],
    profile: GroupProfile,
) -> ValidatedFinalField:
    """编译单字段 output，并从来源身份确定性补齐别名与同组排除边界。"""

    if _normalize_term(suggestion.name) != _normalize_term(plan.name):
        raise ValueError(
            f"最终字段名称必须保持身份计划绑定：期望={plan.name!r}，实际={suggestion.name!r}。"
        )
    source_members = [profile.member(candidate_id) for candidate_id in plan.source_candidate_ids]
    source_aliases = [
        value
        for member in source_members
        for value in (member.definition.get("name"), *(member.definition.get("aliases") or []))
        if isinstance(value, str)
    ]
    source_not_meaning = [
        value
        for member in source_members
        for value in (member.definition.get("not_meaning") or [])
        if isinstance(value, str)
    ]
    aliases = _unique_strings(
        [value for value in source_aliases if not FIELD_ID_PATTERN.fullmatch(value)],
        exclude=(suggestion.name,),
    )[:12]
    sibling_names = [item.name for item in sibling_plans if item.plan_id != plan.plan_id]
    not_meaning = _unique_strings(
        [*source_not_meaning, *sibling_names], exclude=(suggestion.name, *aliases)
    )[:12]
    if len(sibling_plans) > 1 and not not_meaning:
        raise ValueError(f"最终字段 {suggestion.field_id} 必须声明同组相邻事实的排除边界。")
    record = {
        "field_id": suggestion.field_id,
        "name": suggestion.name,
        "meaning": suggestion.meaning,
        "aliases": aliases,
        "not_meaning": not_meaning,
        "output": compile_output_description(suggestion.output),
        "extraction_rule": suggestion.extraction_rule,
    }
    return _validate_definition_record(
        record=record, source_candidate_ids=plan.source_candidate_ids
    )


def validate_batch_field_ids(reports: Sequence[dict[str, Any]]) -> None:
    owners: dict[str, str] = {}
    conflicts: dict[str, set[str]] = {}
    for report in reports:
        if report.get("status") != "succeeded":
            continue
        group_id = str(report["group_id"])
        for field in report.get("final_fields", []):
            field_id = str(field["definition"]["field_id"])
            previous = owners.setdefault(field_id, group_id)
            if previous != group_id:
                conflicts.setdefault(field_id, {previous}).add(group_id)
    if conflicts:
        rendered = {field_id: sorted(groups) for field_id, groups in conflicts.items()}
        raise ValueError(f"不同分组生成了重复最终 field_id：{rendered}。")


def _render_global_gate_card(record: dict[str, Any]) -> str:
    """全局门禁只比较字段身份和值边界，不注入可能诱发“同段共现”误判的提取规则。"""

    output = record["output"]
    lines = [
        f"字段：{record['field_id']}｜{record['name']}",
        f"含义：{record['meaning']}",
        f"顶层输出类型：{output.get('type')}",
    ]
    aliases = record.get("aliases")
    if isinstance(aliases, list) and aliases:
        lines.append("等价称谓：" + "、".join(str(value) for value in aliases))
    properties = output.get("properties")
    if isinstance(properties, dict):
        lines.append("对象子字段：")
        for property_id, child in properties.items():
            if not isinstance(child, dict):
                continue
            lines.append(
                f"- {property_id}｜{child.get('name') or property_id}："
                f"{child.get('meaning') or '未提供子字段含义'}"
            )
    return "\n".join(lines)


def build_global_semantic_gate_prompt(
    *,
    final_fields: Sequence[dict[str, Any]],
    fixed_definitions: Sequence[FieldDefinition],
    current_field_ref: str,
) -> str:
    cards = []
    for item in final_fields:
        reference = f"{item['group_id']}:{item['definition']['field_id']}"
        cards.append(
            f"最终字段引用：{reference}\n"
            + _render_global_gate_card(item["definition"])
        )
    fixed_cards = (
        "\n\n".join(
            _render_global_gate_card(field_definition_record(item))
            for item in fixed_definitions
        )
        if fixed_definitions
        else "- 固定目录为空。"
    )
    fixed_refs = "、".join(f"fixed:{item.field_id}" for item in fixed_definitions) or "无"
    final_refs = "、".join(
        f"{item['group_id']}:{item['definition']['field_id']}" for item in final_fields
    )
    return "\n\n".join(
        (
            "【任务】\n对全部组级字段草案执行最后的跨组语义门禁。只报告冲突，不改写字段。",
            "【状态】\n"
            "- accepted：未被固定字段覆盖，也不与其他最终字段重复或形成需人工处理的重叠。\n"
            "- covered_by_fixed：整体或实质语义已被固定字段/其子字段覆盖；target_ref 填 fixed:<field_id>。\n"
            "- duplicate_final：与另一个最终字段是同一顶层业务事实；target_ref 填对方最终字段引用。\n"
            "- overlap_review：与另一个最终字段边界重叠但不能安全自动合并；target_ref 填对方引用。\n"
            "同一事实的原文版、结构化版和查询切片应视为重复；output 类型差异不是独立字段依据。"
            "只有目标字段可以完整回答当前字段同一个业务问题、提供同一规范值时，才是覆盖或重复。"
            "同处一段、经常共现、互为触发条件、履约上相关或复用同一原文，都不构成语义覆盖；"
            "例如合同金额不覆盖付款方式，合同有效期不覆盖迟延交货责任。"
            "程序已经绑定一个当前字段，你只输出它的一次判断。accepted 的 target_ref 必须为 null；"
            "其他状态必须从下方固定字段或其他最终字段引用中选择合法 target_ref。",
            "【合法固定引用】\n" + fixed_refs,
            "【合法最终字段引用】\n" + final_refs,
            "【固定字段】\n" + fixed_cards,
            "【全部最终字段】\n" + "\n\n".join(cards),
            "【当前只判断】\n" + current_field_ref,
            "【输出】\n只输出当前字段的 target_ref、reason、status，不得输出 final_field_ref 或列表。",
        )
    )


def validate_single_global_semantic_decision(
    *,
    response: SingleGlobalSemanticDecision,
    current_field_ref: str,
    final_field_refs: set[str],
    fixed_definitions: Sequence[FieldDefinition],
) -> GlobalSemanticDecision:
    fixed_refs = {f"fixed:{definition.field_id}" for definition in fixed_definitions}
    target_ref = response.target_ref
    # 模型偶尔漏写引用命名空间；只在引用可唯一解析时由程序规范化，不猜测目标语义。
    if target_ref is not None and ":" not in target_ref:
        fixed_candidate = f"fixed:{target_ref}"
        final_candidates = {
            reference for reference in final_field_refs if reference.endswith(f":{target_ref}")
        }
        if fixed_candidate in fixed_refs and not final_candidates:
            target_ref = fixed_candidate
        elif len(final_candidates) == 1 and fixed_candidate not in fixed_refs:
            target_ref = next(iter(final_candidates))
    if response.status == "accepted":
        if target_ref is not None:
            raise ValueError(f"{current_field_ref} accepted 时 target_ref 必须为空。")
    elif response.status == "covered_by_fixed":
        if target_ref not in fixed_refs:
            raise ValueError(
                f"{current_field_ref} 未引用有效固定字段：{target_ref!r}。"
            )
    elif target_ref not in final_field_refs - {current_field_ref}:
        raise ValueError(
            f"{current_field_ref} 未引用有效的其他最终字段：{target_ref!r}。"
        )
    return GlobalSemanticDecision(
        final_field_ref=current_field_ref,
        target_ref=target_ref,
        reason=response.reason,
        status=response.status,
    )


def build_global_conflict_confirmation_prompt(
    *,
    current_field: dict[str, Any],
    target_field: dict[str, Any],
    proposed_decision: GlobalSemanticDecision,
) -> str:
    """对全局门禁的非 accepted 结论做一次聚焦复核，隔离“业务相关即覆盖”的误判。"""

    return "\n\n".join(
        (
            "【任务】\n复核一个疑似字段冲突。只有两个字段回答同一个业务问题、目标可以提供当前"
            "字段同一规范值，或二者确实无法建立清晰独立边界时，才能确认冲突。",
            "【严格排除】\n同处一段、经常共现、互为触发条件、共享‘交付/付款/争议’上位主题、"
            "一个字段是另一个履约判断的前提、或复用同一原文，都只是业务相关，必须判 false_positive。"
            "例如交付地点不覆盖交付方式或物流服务商，争议解决方式不覆盖管辖地。",
            "【当前字段】\n" + _render_global_gate_card(current_field),
            "【疑似目标】\n" + _render_global_gate_card(target_field),
            "【初判】\n"
            f"status={proposed_decision.status}\nreason={proposed_decision.reason}",
            "【输出】\nreason 先逐项比较两个字段最终保存的规范值；随后 status 只允许"
            "confirmed_conflict 或 false_positive。",
        )
    )


def validate_global_semantic_gate(
    *,
    response: GlobalSemanticGateResponse,
    final_fields: Sequence[dict[str, Any]],
    fixed_definitions: Sequence[FieldDefinition],
) -> dict[str, Any]:
    refs = {
        f"{item['group_id']}:{item['definition']['field_id']}" for item in final_fields
    }
    fixed_refs = {f"fixed:{definition.field_id}" for definition in fixed_definitions}
    actual = [item.final_field_ref for item in response.decisions]
    if len(actual) != len(set(actual)):
        raise ValueError("全局语义门禁中同一 final_field_ref 不得重复。")
    if set(actual) != refs:
        raise ValueError(
            f"全局语义门禁必须覆盖全部最终字段。缺少={sorted(refs - set(actual))}；"
            f"额外={sorted(set(actual) - refs)}"
        )
    decisions = []
    for item in response.decisions:
        if item.status == "accepted":
            if item.target_ref is not None:
                raise ValueError(f"{item.final_field_ref} accepted 时 target_ref 必须为空。")
        elif item.status == "covered_by_fixed":
            if item.target_ref not in fixed_refs:
                raise ValueError(f"{item.final_field_ref} 未引用有效固定字段：{item.target_ref!r}。")
        elif item.target_ref not in refs - {item.final_field_ref}:
            raise ValueError(f"{item.final_field_ref} 未引用有效的其他最终字段：{item.target_ref!r}。")
        decisions.append(item.model_dump(mode="json"))
    conflicts = [item for item in decisions if item["status"] != "accepted"]
    return {
        "status": "passed" if not conflicts else "failed",
        "decision_count": len(decisions),
        "conflict_count": len(conflicts),
        "decisions": decisions,
    }


def _validate_definition_record(
    *, record: dict[str, Any], source_candidate_ids: Sequence[str]
) -> ValidatedFinalField:
    field_id = record.get("field_id")
    name = record.get("name")
    meaning = record.get("meaning")
    output = record.get("output")
    extraction_rule = record.get("extraction_rule")
    if not isinstance(field_id, str) or not FIELD_ID_PATTERN.fullmatch(field_id):
        raise ValueError(f"最终字段 {field_id!r} 必须使用 lower_snake_case。")
    if not isinstance(name, str) or name.strip() in GENERIC_FIELD_NAMES:
        raise ValueError(f"最终字段 {field_id} 名称过于宽泛或缺失。")
    if any(character.isdigit() for character in name):
        raise ValueError(f"最终字段 {field_id} 名称不得包含具体编号或数值。")
    if not isinstance(meaning, str) or len(meaning.strip()) < 10:
        raise ValueError(f"最终字段 {field_id} meaning 不完整。")
    if not isinstance(output, dict):
        raise ValueError(f"最终字段 {field_id} output 必须是对象。")
    _validate_output_definition(output)
    if not isinstance(extraction_rule, str):
        raise ValueError(f"最终字段 {field_id} extraction_rule 必须是字符串。")
    generalized_rule = validate_generalized_extraction_rule(extraction_rule)

    def normalized_string_list(key: str) -> list[str]:
        values = record.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"最终字段 {field_id} {key} 必须是字符串数组。")
        return _unique_strings(values, exclude=(str(name),))

    normalized_record = {
        "field_id": field_id,
        "name": name.strip(),
        "meaning": meaning.strip(),
        "aliases": normalized_string_list("aliases"),
        "not_meaning": normalized_string_list("not_meaning"),
        "output": output,
        "extraction_rule": generalized_rule,
        "examples": [],
    }
    try:
        definition = YamlFieldCatalog._to_definition(normalized_record, FieldKind.ATTRIBUTE)
        extraction_schema = build_field_extraction_schema(
            [normalized_record], field_set_name="DiscoveryRefinement"
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"最终字段 {field_id} 未通过字段定义契约：{error}") from error
    return ValidatedFinalField(
        definition=definition,
        definition_record=normalized_record,
        source_candidate_ids=tuple(source_candidate_ids),
        extraction_schema=extraction_schema,
    )


def finalize_singleton_group(profile: GroupProfile) -> dict[str, Any]:
    if len(profile.members) != 1:
        raise ValueError("确定性单候选分支只能处理恰好一个成员的分组。")
    member = profile.members[0]
    field = _validate_definition_record(
        record=member.definition, source_candidate_ids=(member.candidate_id,)
    )
    return {
        "group_id": profile.group_id,
        "reason": "该组只有一个已通过候选门禁的字段身份，无组内合并或拆分对象。",
        "decision": "passthrough_singleton",
        "final_fields": [field.report()],
        "discarded_candidates": [],
        "input_candidate_ids": [member.candidate_id],
    }


def finalize_group_reason(*, reason: str, decision: GroupDecision) -> str:
    return _finalize_reason(
        reason=reason,
        expected=decision,
        pattern=GROUP_REASON_SUFFIX_PATTERN,
        label="decision",
    )


def finalize_discard_reason(*, reason: str, disposition: DISPOSITION) -> str:
    return _finalize_reason(
        reason=reason,
        expected=disposition,
        pattern=DISCARD_REASON_SUFFIX_PATTERN,
        label="disposition",
    )


def _finalize_reason(
    *, reason: str, expected: str, pattern: re.Pattern[str], label: str
) -> str:
    normalized = reason.strip()
    suffix = pattern.search(normalized)
    if suffix:
        declared = next(iter(suffix.groupdict().values()))
        if declared != expected:
            raise ValueError(
                f"reason 的固定结尾与 {label} 不一致：reason={declared}，{label}={expected}。"
            )
        normalized = normalized[: suffix.start()].rstrip("。！？.! \t\r\n")
    else:
        normalized = normalized.rstrip("。！？.! \t\r\n")
    if not normalized:
        raise ValueError(f"reason 必须在最终 {label} 结论前说明具体依据。")
    return f"{normalized}。因此 {label}={expected}"
