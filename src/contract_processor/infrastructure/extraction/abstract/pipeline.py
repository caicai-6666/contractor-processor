"""直接读取原始 PDF 的固定六栏目合同摘要算法。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

try:
    from contract_processor.application.prompts.pdf_prefix import (
        SYSTEM_MESSAGE,
        build_common_prefix,
    )
    from contract_processor.async_utils import run_blocking
    from contract_processor.infrastructure.extraction.stage_result import StageResult
    from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
    from jsonschema import Draft202012Validator
    from openai import AsyncOpenAI
    from pydantic import BaseModel, ConfigDict, Field
    import yaml
except ImportError as error:  # 在模型调用前检查依赖。
    raise RuntimeError(
        "缺少 Abstract 抽取依赖。请在已激活的环境执行：\n"
        "python -m pip install -e .\n"
        f"原始错误：{error}"
    ) from error


INITIAL_MAX_COMPLETION_TOKENS = 6144
RETRY_MAX_COMPLETION_TOKENS = 2048
SUMMARY_TEMPERATURE = 0.1
SECTION_IDS = (
    "contract_number",
    "contract_title",
    "parties",
    "time",
    "main_content",
    "key_performance_terms",
)
DISALLOWED_TOOL_PROTOCOL_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<tool_response>",
    "</tool_response>",
)
PLACEHOLDER_WORDS = ("未知", "未发现", "暂无", "不详", "无相关信息", "无明确")
SENSITIVE_PARTY_PATTERNS = (
    "开户行",
    "账号",
    "帐号",
    "税号",
    "统一社会信用代码",
    "法定代表人",
    "委托代理人",
    "联系人",
)
SENSITIVE_TIME_PATTERNS = SENSITIVE_PARTY_PATTERNS + ("银行",)
TIME_TYPE_LABELS = {
    "signing_date": "签订日期",
    "effective_date": "生效日期",
    "effective_info": "生效信息",
    "validity_period": "合同有效期",
}
TIME_TYPE_RANKS = {
    time_type: rank for rank, time_type in enumerate(TIME_TYPE_LABELS)
}
PERFORMANCE_TYPE_LABELS = {
    "payment": "付款",
    "delivery_service": "交付/服务",
    "acceptance": "验收",
    "quality_warranty": "质量/质保",
    "breach_termination": "违约/解除",
    "dispute_resolution": "争议解决",
}
PERFORMANCE_TYPE_RANKS = {
    performance_type: rank
    for rank, performance_type in enumerate(PERFORMANCE_TYPE_LABELS)
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationIssue(StrictModel):
    """同时服务程序审计和模型纠错，不把内部异常直接暴露给模型。"""

    code: str
    internal_message: str
    retry_guidance: str


def validation_issue(
    code: str, internal_message: str, retry_guidance: str
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        internal_message=internal_message,
        retry_guidance=retry_guidance,
    )


def add_validation_issue(
    errors: list[ValidationIssue],
    code: str,
    internal_message: str,
    retry_guidance: str,
) -> None:
    errors.append(validation_issue(code, internal_message, retry_guidance))


SectionStatus = Literal[
    "found", "not_found", "ambiguous", "conflicting", "not_applicable"
]


class SummarySectionBase(StrictModel):
    """每个栏目都先保留证据，再给出判断。"""

    evidence_text: str | None = Field(max_length=2400)
    page_refs: list[int] = Field(max_length=20)
    reason: str = Field(min_length=1, max_length=240)
    status: SectionStatus


class ScalarSummarySection(SummarySectionBase):
    """合同编号、标题和主要内容使用单个摘要文本。"""

    summary_text: str | None = Field(max_length=1000)


class PartySummaryItem(StrictModel):
    """相关方的角色与主体名称分开承载，展示标点由程序生成。"""

    role: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)


class PartySummarySection(SummarySectionBase):
    summary_items: list[PartySummaryItem] | None = Field(max_length=20)


TimeItemType = Literal[
    "signing_date", "effective_date", "effective_info", "validity_period"
]


class TimeSummaryItem(StrictModel):
    """时间类型由 Schema 约束，展示标签由程序确定性生成。"""

    type: TimeItemType
    text: str = Field(min_length=1, max_length=500)


class TimeSummarySection(SummarySectionBase):
    """时间栏目使用类型化项目，不在自由文本中编码标签。"""

    summary_items: list[TimeSummaryItem] | None = Field(max_length=10)


PerformanceItemType = Literal[
    "payment",
    "delivery_service",
    "acceptance",
    "quality_warranty",
    "breach_termination",
    "dispute_resolution",
]


class PerformanceSummaryItem(StrictModel):
    """履约类别与事实正文分离，避免自由字符串标签造成格式误判。"""

    type: PerformanceItemType
    text: str = Field(min_length=1, max_length=1000)


class PerformanceSummarySection(SummarySectionBase):
    summary_items: list[PerformanceSummaryItem] | None = Field(max_length=20)


SummarySection = (
    ScalarSummarySection
    | PartySummarySection
    | TimeSummarySection
    | PerformanceSummarySection
)


class DirectPdfSummaryCandidate(StrictModel):
    contract_number: ScalarSummarySection
    contract_title: ScalarSummarySection
    parties: PartySummarySection
    time: TimeSummarySection
    main_content: ScalarSummarySection
    key_performance_terms: PerformanceSummarySection


def _read_text_sync(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _load_yaml_object_sync(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        content = yaml.safe_load(file)
    if not isinstance(content, dict):
        raise ValueError(f"YAML 根节点必须为对象：{path}")
    return content


def render_template(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for key, value in replacements.items():
        marker = "{{" + key + "}}"
        if marker not in rendered:
            raise ValueError(f"提示词缺少占位符：{marker}")
        rendered = rendered.replace(marker, value)
    if re.search(r"\{\{[A-Z0-9_]+\}\}", rendered):
        raise ValueError("提示词仍包含未替换占位符。")
    return rendered


def messages_for(
    common_prefix: str, images: list[dict[str, Any]], task_suffix: str
) -> list[dict[str, Any]]:
    """稳定公共文本与全部图像在前，任务后缀在后，以复用多模态前缀。"""

    content: list[dict[str, Any]] = [{"type": "text", "text": common_prefix}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image["data_url"]}}
        for image in images
    )
    content.append({"type": "text", "text": task_suffix})
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": content},
    ]


def response_format(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def build_sampling_parameters(generation: dict[str, Any]) -> dict[str, Any]:
    return {
        "temperature": SUMMARY_TEMPERATURE,
        "top_p": float(generation["top_p"]),
        "presence_penalty": 0.0,
        "seed": int(generation["seed"]),
        "extra_body": {
            "top_k": int(generation["top_k"]),
            "repetition_penalty": float(generation["repetition_penalty"]),
            "bad_words": list(DISALLOWED_TOOL_PROTOCOL_MARKERS),
        },
    }


async def invoke_json(
    *,
    client: AsyncOpenAI,
    model: str,
    common_prefix: str,
    images: list[dict[str, Any]],
    task_suffix: str,
    schema_model: type[BaseModel],
    schema_name: str,
    generation: dict[str, Any],
    max_completion_tokens: int,
    model_request_limiter: ModelRequestLimiter,
) -> tuple[BaseModel, dict[str, Any]]:
    schema = schema_model.model_json_schema()
    started_at = time.perf_counter()
    async with model_request_limiter.slot():
        completion = await client.chat.completions.create(
            model=model,
            messages=messages_for(common_prefix, images, task_suffix),
            response_format=response_format(schema, schema_name),
            max_completion_tokens=max_completion_tokens,
            **build_sampling_parameters(generation),
        )
    metrics = {
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "common_prefix_characters": len(common_prefix),
        "task_suffix_characters": len(task_suffix),
        "image_count": len(images),
        "image_bytes": sum(image["image_bytes"] for image in images),
    }
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("模型未返回 JSON 内容。")
    try:
        decoded = json.loads(content)
        Draft202012Validator(schema).validate(decoded)
        parsed = schema_model.model_validate(decoded)
    except Exception as error:
        finish_reason = completion.choices[0].finish_reason
        raise RuntimeError(
            "模型响应未通过 JSON Schema 校验；"
            f"finish_reason={finish_reason!r}。"
        ) from error
    return parsed, metrics


def normalize_evidence_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def validate_page_refs(
    page_refs: list[int], page_count: int, section_id: str
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    if page_refs != sorted(page_refs):
        errors.append(
            validation_issue(
                "PAGE_REFS_NOT_SORTED",
                f"{section_id}: page_refs 必须升序",
                "你提供的证据页码顺序不正确。请按从小到大的物理 PDF 页码重新排列，不要改变证据事实。",
            )
        )
    if len(page_refs) != len(set(page_refs)):
        errors.append(
            validation_issue(
                "PAGE_REFS_DUPLICATED",
                f"{section_id}: page_refs 不得重复",
                "你重复引用了同一物理 PDF 页码。请将重复页码去重，每个证据页只保留一次。",
            )
        )
    invalid = [page for page in page_refs if not 1 <= page <= page_count]
    if invalid:
        errors.append(
            validation_issue(
                "PAGE_REFS_OUT_OF_RANGE",
                f"{section_id}: page_refs 包含不可见页码 {invalid}",
                f"你引用了合同实际范围之外的页码 {invalid}。本 PDF 的物理页码范围是 1 至 {page_count}；请重新定位证据并只填写可见页码。",
            )
        )
    return errors


def validate_section(
    section_id: str,
    section: SummarySection,
    page_count: int,
    section_policy: dict[str, Any],
) -> list[ValidationIssue]:
    errors = validate_page_refs(section.page_refs, page_count, section_id)
    evidence = (section.evidence_text or "").strip()
    if isinstance(section, ScalarSummarySection):
        summary_values = [section.summary_text.strip()] if section.summary_text else []
        output_is_null = section.summary_text is None
        output_name = "summary_text"
    elif isinstance(section, PartySummarySection):
        party_items = section.summary_items or []
        summary_values = [
            f"{item.role.strip()}：{item.name.strip()}" for item in party_items
        ]
        output_is_null = section.summary_items is None
        output_name = "summary_items"
        identities = [
            (item.role.strip(), item.name.strip()) for item in party_items
        ]
        if any(not role or not name for role, name in identities):
            add_validation_issue(
                errors,
                "PARTY_ROLE_OR_NAME_EMPTY",
                "parties: summary_items 的 role 和 name 不得为空",
                "相关方项目中有角色或主体名称为空。请重新阅读 PDF，每项分别填写明确角色和一个完整主体名称；无法可靠确定时不要输出该项目。",
            )
        if len(identities) != len(set(identities)):
            add_validation_issue(
                errors,
                "PARTY_DUPLICATED",
                "parties: summary_items 不得包含重复主体",
                "你重复输出了相同角色和主体。请按角色与主体去重，每个相同组合只保留一次，不得删除不同角色下的真实主体。",
            )
        if any("\n" in item.role or "\n" in item.name for item in party_items):
            add_validation_issue(
                errors,
                "PARTY_FIELD_MULTILINE",
                "parties: summary_items 的 role 和 name 必须是单行",
                "相关方的角色或主体名称包含换行。请将每个 role 和 name 分别整理为单行文本，一个项目只表达一个主体。",
            )
    elif isinstance(section, TimeSummarySection):
        time_items = section.summary_items or []
        summary_values = [
            f"{TIME_TYPE_LABELS[item.type]}：{item.text.strip()}" for item in time_items
        ]
        output_is_null = section.summary_items is None
        output_name = "summary_items"
        identities = [(item.type, item.text.strip()) for item in time_items]
        if any(not item.text.strip() for item in time_items):
            add_validation_issue(
                errors,
                "TIME_TEXT_EMPTY",
                "time: summary_items 的 text 不得为空",
                "时间项目的事实正文为空。请重新定位合同级时间原文并填写 text；没有可靠事实时不要输出该项目。",
            )
        if len(identities) != len(set(identities)):
            add_validation_issue(
                errors,
                "TIME_ITEM_DUPLICATED",
                "time: summary_items 不得包含重复项目",
                "你重复输出了完全相同的时间事实。请去重，每个相同时间事实只保留一次。",
            )
        time_types = [item.type for item in time_items]
        if len(time_types) != len(set(time_types)):
            add_validation_issue(
                errors,
                "TIME_TYPE_DUPLICATED",
                "time: 每种时间类型最多输出一项",
                "你为同一种合同级时间类型输出了多项。请合并同类事实，每种 type 最多保留一项；合并时不得增加 PDF 中没有的内容。",
            )
        if [TIME_TYPE_RANKS[item_type] for item_type in time_types] != sorted(
            TIME_TYPE_RANKS[item_type] for item_type in time_types
        ):
            add_validation_issue(
                errors,
                "TIME_TYPE_ORDER_INVALID",
                "time: summary_items 必须按固定时间类型顺序输出",
                "时间项目顺序不正确。请依次按签订日期、生效日期、生效信息、合同有效期排列，只输出 PDF 中有可靠证据的类型。",
            )
        if any("\n" in item.text for item in time_items):
            add_validation_issue(
                errors,
                "TIME_TEXT_MULTILINE",
                "time: summary_items 的 text 必须是单行",
                "某个时间项目的 text 包含换行。请把每项时间事实整理为单行正文。",
            )
        if any(
            item.text.strip().startswith(tuple(TIME_TYPE_LABELS.values()))
            for item in time_items
        ):
            add_validation_issue(
                errors,
                "TIME_TEXT_REPEATS_LABEL",
                "time: summary_items 的 text 不得重复中文展示标签",
                "时间项目的 text 重复写入了“签订日期”等展示标签。请只保留事实正文，中文标签和冒号由程序根据 type 自动生成。",
            )
    else:
        assert isinstance(section, PerformanceSummarySection)
        performance_items = section.summary_items or []
        summary_values = [
            f"{PERFORMANCE_TYPE_LABELS[item.type]}：{item.text.strip()}"
            for item in performance_items
        ]
        output_is_null = section.summary_items is None
        output_name = "summary_items"
        identities = [
            (item.type, item.text.strip()) for item in performance_items
        ]
        if any(not text for _, text in identities):
            add_validation_issue(
                errors,
                "PERFORMANCE_TEXT_EMPTY",
                "key_performance_terms: summary_items 的 text 不得为空",
                "履约项目的事实正文为空。请重新定位该类履约约定并填写 text；没有可靠约定时不要输出该类型。",
            )
        if len(identities) != len(set(identities)):
            add_validation_issue(
                errors,
                "PERFORMANCE_ITEM_DUPLICATED",
                "key_performance_terms: summary_items 不得包含重复项目",
                "你重复输出了完全相同的履约事实。请去重，每个相同项目只保留一次。",
            )
        performance_types = [item.type for item in performance_items]
        if len(performance_types) != len(set(performance_types)):
            add_validation_issue(
                errors,
                "PERFORMANCE_TYPE_DUPLICATED",
                "key_performance_terms: 每种履约类型最多输出一项",
                "你为同一种履约类别输出了多项。请合并同类约定，每种 type 最多保留一项，并保留重要期限、比例、条件和责任主体。",
            )
        if [
            PERFORMANCE_TYPE_RANKS[performance_type]
            for performance_type in performance_types
        ] != sorted(
            PERFORMANCE_TYPE_RANKS[performance_type]
            for performance_type in performance_types
        ):
            add_validation_issue(
                errors,
                "PERFORMANCE_TYPE_ORDER_INVALID",
                "key_performance_terms: 各类型必须按固定语义顺序输出",
                "履约项目顺序不正确。请依次按付款、交付/服务、验收、质量/质保、违约/解除、争议解决排列，没有可靠内容的类型直接省略。",
            )
        if any("\n" in item.text for item in performance_items):
            add_validation_issue(
                errors,
                "PERFORMANCE_TEXT_MULTILINE",
                "key_performance_terms: summary_items 的 text 必须是单行",
                "某个履约项目的 text 包含换行。请把该类型的事实压缩为单行正文，类别标签和换行由程序处理。",
            )
    summary = "\n".join(summary_values)
    if section.status == "found":
        if not evidence:
            add_validation_issue(
                errors,
                "FOUND_WITHOUT_EVIDENCE",
                f"{section_id}: found 必须包含 evidence_text",
                "你判断该栏目已经找到可靠结果，但没有提供可核对的原文证据。请重新阅读 PDF：有依据时补充最小必要原文和页码，否则改为合适的非 found 状态。",
            )
        if not section.page_refs:
            add_validation_issue(
                errors,
                "FOUND_WITHOUT_PAGE_REFS",
                f"{section_id}: found 必须包含 page_refs",
                "你判断该栏目已经找到可靠结果，但没有填写证据所在页码。请重新定位原文，并按物理 PDF 页码填写 page_refs。",
            )
        if not summary_values:
            add_validation_issue(
                errors,
                "FOUND_WITHOUT_SUMMARY",
                f"{section_id}: found 必须包含 {output_name}",
                "你判断该栏目已经找到可靠结果，但摘要内容为空。请根据证据填写该栏摘要；若无法形成可靠摘要，应改为合适的非 found 状态。",
            )
    else:
        if not output_is_null:
            add_validation_issue(
                errors,
                "NON_FOUND_WITH_SUMMARY",
                f"{section_id}: {section.status} 的 {output_name} 必须为 null",
                f"你将该栏目判断为 {section.status}，却仍输出了摘要内容。非 found 状态下摘要必须为 null，不得保留推测或候选结论。",
            )
        if section.status in {"ambiguous", "conflicting"} and not evidence:
            add_validation_issue(
                errors,
                "UNCERTAIN_WITHOUT_EVIDENCE",
                f"{section_id}: {section.status} 必须保留候选 evidence_text",
                f"你将该栏目判断为 {section.status}，但没有保留导致歧义或冲突的候选原文。请摘录相关候选证据并标注页码，摘要仍保持 null。",
            )
        if bool(evidence) != bool(section.page_refs):
            add_validation_issue(
                errors,
                "EVIDENCE_PAGE_REFS_MISMATCH",
                f"{section_id}: evidence_text 与 page_refs 必须同时存在或同时为空",
                "原文证据与证据页码没有成对出现。有 evidence_text 时必须提供对应物理页码；没有证据时，两者都应为空。",
            )

    for placeholder in PLACEHOLDER_WORDS:
        if placeholder in summary:
            if isinstance(section, PerformanceSummarySection):
                retry_guidance = (
                    f"履约项目使用了占位词“{placeholder}”。"
                    "没有可靠约定的 type 必须从 summary_items 中删除；"
                    "保留其他已有原文支持的项目，不要把整个栏目改为非 found。"
                )
            else:
                retry_guidance = (
                    f"摘要中使用了占位词“{placeholder}”。有可靠事实时直接写事实；"
                    "没有可靠事实时使用合适的非 found 状态并将摘要设为 null。"
                )
            add_validation_issue(
                errors,
                "SUMMARY_CONTAINS_PLACEHOLDER",
                f"{section_id}: 摘要不得包含占位词“{placeholder}”",
                retry_guidance,
            )

    if section_id == "contract_number" and section.status == "found":
        assert isinstance(section, ScalarSummarySection)
        normalized_summary = normalize_evidence_text(summary)
        normalized_evidence = normalize_evidence_text(evidence)
        if "\n" in summary or normalized_summary not in normalized_evidence:
            add_validation_issue(
                errors,
                "CONTRACT_NUMBER_NOT_VERBATIM",
                "contract_number: summary_text 必须是证据中的单行原始编号",
                "你输出的合同编号无法在所给证据中逐字核对，或包含了多行内容。请重新读取合同自身明确标注的单行编号；不得使用文件名、项目号、订单号或其他编号替代。",
            )
    if section_id == "parties" and summary:
        assert isinstance(section, PartySummarySection)
        for pattern in SENSITIVE_PARTY_PATTERNS:
            if pattern in summary:
                add_validation_issue(
                    errors,
                    "PARTY_CONTAINS_NON_PARTY_DETAILS",
                    f"parties: summary_items 不得包含“{pattern}”",
                    f"相关方栏目混入了“{pattern}”等非合同主体资料。这里只保留承担合同权利义务的组织或个人及其明确角色，删除代表人、联系人、银行、账号和税务信息。",
                )
    if section_id == "time" and summary:
        assert isinstance(section, TimeSummarySection)
        for pattern in SENSITIVE_TIME_PATTERNS:
            if pattern in summary:
                add_validation_issue(
                    errors,
                    "TIME_CONTAINS_NON_TIME_DETAILS",
                    f"time: summary_items 不得包含“{pattern}”",
                    f"时间栏目混入了“{pattern}”等非合同级时间信息。请只保留签订、生效或合同整体有效期事实，删除主体、代表人、银行和账号资料。",
                )
    if bool(section_policy.get("required")) and section.status != "found":
        add_validation_issue(
            errors,
            "REQUIRED_SECTION_NOT_FOUND",
            f"{section_id}: 必填栏目必须为 found",
            "当前栏目被定义为必填，但你没有返回 found。请重新完整检查 PDF；只有找到可核对证据时才能返回 found，不得为了满足必填要求而捏造内容。",
        )
    return errors


def validate_candidate(
    candidate: DirectPdfSummaryCandidate,
    page_count: int,
    policy: dict[str, Any],
) -> dict[str, list[ValidationIssue]]:
    return {
        section_id: errors
        for section_id in SECTION_IDS
        if (
            errors := validate_section(
                section_id,
                getattr(candidate, section_id),
                page_count,
                policy["sections"][section_id],
            )
        )
    }


def validation_issues_to_json(
    errors: dict[str, list[ValidationIssue]],
) -> dict[str, list[dict[str, str]]]:
    return {
        section_id: [issue.model_dump(mode="json") for issue in issues]
        for section_id, issues in errors.items()
    }


def render_retry_feedback(issues: list[ValidationIssue]) -> str:
    """只向模型提供可执行的自然语言纠错指导，隐藏内部错误表达。"""

    return "\n".join(
        f"- 问题 {index}：{issue.retry_guidance}"
        for index, issue in enumerate(issues, start=1)
    )


def render_final_summary(
    candidate: DirectPdfSummaryCandidate, policy: dict[str, Any]
) -> str:
    blocks: list[str] = []
    for section_id in SECTION_IDS:
        title = policy["sections"][section_id]["title"]
        section = getattr(candidate, section_id)
        if section.status != "found":
            content = ""
        elif isinstance(section, ScalarSummarySection):
            content = (section.summary_text or "").strip()
        elif isinstance(section, PartySummarySection):
            content = "\n".join(
                f"{item.role.strip()}：{item.name.strip()}"
                for item in section.summary_items or []
            )
        elif isinstance(section, TimeSummarySection):
            content = "\n".join(
                f"{TIME_TYPE_LABELS[item.type]}：{item.text.strip()}"
                for item in section.summary_items or []
            )
        else:
            assert isinstance(section, PerformanceSummarySection)
            content = "\n".join(
                f"{PERFORMANCE_TYPE_LABELS[item.type]}：{item.text.strip()}"
                for item in section.summary_items or []
            )
        block = f"【{title}】"
        if content:
            block += f"\n{content}"
        blocks.append(block)
    return "\n\n".join(blocks).strip() + "\n"


async def run_abstract_extraction(
    *,
    project_root_path: Path,
    pdf_path: Path,
    document_id: str,
    shared_images: list[dict[str, Any]],
    shared_source_page_count: int,
    shared_client: AsyncOpenAI,
    model_request_limiter: ModelRequestLimiter,
) -> StageResult[dict[str, Any]]:
    """异步生成固定摘要，通过内存返回候选、校验与指标。"""

    root, pdf_path = await asyncio.gather(
        run_blocking(project_root_path.resolve),
        run_blocking(pdf_path.resolve),
    )
    if not await run_blocking(pdf_path.is_file):
        raise FileNotFoundError(f"找不到待生成摘要的 PDF：{pdf_path}")

    settings = await run_blocking(
        _load_yaml_object_sync, root / "configs/settings.yaml"
    )
    mllm = settings["models"]["mllm"]
    generation = mllm["generation"]
    policy = await run_blocking(
        _load_yaml_object_sync, root / settings["paths"]["contract_summary_policy"]
    )
    max_model_len = int(mllm["context_window_tokens"])
    images = shared_images
    page_count = shared_source_page_count

    prompt_dir = Path(__file__).parent / "prompts"
    common_prefix = await build_common_prefix(page_count)
    initial_prompt, retry_template = await asyncio.gather(
        run_blocking(
            _read_text_sync, prompt_dir / "01_extract_summary_sections.txt"
        ),
        run_blocking(_read_text_sync, prompt_dir / "02_retry_summary_section.txt"),
    )

    retry_metrics: list[dict[str, Any]] = []
    client = shared_client
    initial_result, initial_metrics = await invoke_json(
        client=client,
        model=mllm["model"],
        common_prefix=common_prefix,
        images=images,
        task_suffix=initial_prompt,
        schema_model=DirectPdfSummaryCandidate,
        schema_name="direct_pdf_contract_summary",
        generation=generation,
        max_completion_tokens=min(
            INITIAL_MAX_COMPLETION_TOKENS, int(generation["max_completion_tokens"])
        ),
        model_request_limiter=model_request_limiter,
    )
    assert isinstance(initial_result, DirectPdfSummaryCandidate)
    candidate = initial_result
    initial_errors = validate_candidate(candidate, page_count, policy)

    max_retries = int(policy.get("max_retries_per_section", 0))
    if max_retries < 0:
        raise ValueError("max_retries_per_section 不得小于 0。")
    if initial_errors and bool(policy.get("retry_invalid_sections")):
        for section_id in SECTION_IDS:
            if section_id not in initial_errors:
                continue
            section_policy = policy["sections"][section_id]
            current_issues = initial_errors[section_id]
            for attempt in range(1, max_retries + 1):
                # 每次只重做失败栏目，避免一个局部问题让模型重写其余已通过事实。
                retry_prompt = render_template(
                    retry_template,
                    {
                        "SECTION_ID": section_id,
                        "SECTION_TITLE": str(section_policy["title"]),
                        "SECTION_INSTRUCTION": str(section_policy["instruction"]),
                        "VALIDATION_FEEDBACK": render_retry_feedback(current_issues),
                    },
                )
                try:
                    retry_result, retry_call_metrics = await invoke_json(
                        client=client,
                        model=mllm["model"],
                        common_prefix=common_prefix,
                        images=images,
                        task_suffix=retry_prompt,
                        schema_model={
                            "scalar": ScalarSummarySection,
                            "party_items": PartySummarySection,
                            "time_items": TimeSummarySection,
                            "performance_items": PerformanceSummarySection,
                        }[section_policy["output_mode"]],
                        schema_name=f"retry_{section_id}_{attempt}",
                        generation=generation,
                        max_completion_tokens=min(
                            RETRY_MAX_COMPLETION_TOKENS,
                            int(generation["max_completion_tokens"]),
                        ),
                        model_request_limiter=model_request_limiter,
                    )
                    assert isinstance(
                        retry_result,
                        (
                            ScalarSummarySection,
                            PartySummarySection,
                            TimeSummarySection,
                            PerformanceSummarySection,
                        ),
                    )
                    candidate = candidate.model_copy(update={section_id: retry_result})
                    section_errors = validate_section(
                        section_id, retry_result, page_count, section_policy
                    )
                    current_issues = section_errors
                    retry_metrics.append(
                        {
                            "section_id": section_id,
                            "attempt": attempt,
                            "status": "succeeded" if not section_errors else "invalid",
                            "metrics": retry_call_metrics,
                        }
                    )
                    if not section_errors:
                        break
                except Exception as error:
                    current_issues = [
                        validation_issue(
                            "RETRY_RESPONSE_INVALID",
                            str(error),
                            "上一次重试没有返回符合当前 JSON Schema 的完整栏目对象。"
                            "请重新阅读 PDF，并严格按照本次 Schema 输出全部必需字段，"
                            "不要输出解释文字或 Markdown。",
                        )
                    ]
                    retry_metrics.append(
                        {
                            "section_id": section_id,
                            "attempt": attempt,
                            "status": "failed",
                            "error": str(error),
                        }
                    )

    final_errors = validate_candidate(candidate, page_count, policy)
    final_summary = render_final_summary(candidate, policy)
    summary_characters = len(final_summary.strip())
    validation = {
        "is_valid": not final_errors,
        "section_errors": validation_issues_to_json(final_errors),
        "summary_characters": summary_characters,
    }
    metrics = {
        "initial": initial_metrics,
        "retries": retry_metrics,
        "summary_characters": summary_characters,
        "initial_invalid_sections": list(initial_errors),
        "final_invalid_sections": list(final_errors),
    }
    if final_errors:
        flattened = [
            issue.internal_message
            for issues in final_errors.values()
            for issue in issues
        ]
        raise RuntimeError("最终摘要业务校验失败：" + "；".join(flattened))

    return StageResult(
        payload={
            "document_id": document_id,
            "sections": candidate.model_dump(mode="json"),
            "text": final_summary,
        },
        validation=validation,
        metrics=metrics,
    )
