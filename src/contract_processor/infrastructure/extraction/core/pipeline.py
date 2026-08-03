"""两步 MLLM Core 字段抽取算法。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal


try:
    from contract_processor.application.prompts.pdf_prefix import (
        SYSTEM_MESSAGE,
        build_common_prefix,
    )
    from contract_processor.application.prompts.core_fields import build_compact_field_prompt
    from contract_processor.application.schemas.core_extraction import (
        build_core_extraction_schema,
    )
    from contract_processor.async_utils import run_blocking
    from contract_processor.domain.enums import FieldKind
    from contract_processor.domain.effective_mechanism import (
        effective_date_has_provenance,
    )
    from contract_processor.domain.identifiers import SHA256_DOCUMENT_ID_PATTERN
    from contract_processor.infrastructure.extraction.stage_result import StageResult
    from contract_processor.infrastructure.extraction.field_values import (
        FieldExtractionCandidate as CoreExtractionCandidate,
        FieldStatus,
        ObjectFieldCandidate,
        ObjectFieldValue,
        ObjectPropertyValue,
        ScalarFieldValue as CoreFieldValue,
        aggregate_field_metrics,
        aggregate_object_status,
        finalize_candidate_field,
        validate_extracted_field,
        validate_property_envelope,
        validate_scalar_field_envelope as validate_field_envelope,
    )
    from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
    from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
    from dotenv import load_dotenv
    from jsonschema import Draft202012Validator
    from openai import AsyncOpenAI
    from pydantic import BaseModel, ConfigDict, Field
    import yaml
except ImportError as error:  # 依赖检查应在发起模型请求前完成，避免产生不完整结果。
    raise RuntimeError(
        "缺少 Core 抽取依赖。请在已激活的环境执行：\n"
        "python -m pip install -e .\n"
        f"原始错误：{error}"
    ) from error


# Step 1 需要穷举金额与费用原文，避免多页价格表被截断。
# Step 2 每次只处理一个字段，使 reason 与标量字段或对象子字段直接对应，并隔离生成干扰。
STEP_1_MAX_COMPLETION_TOKENS = 6144
STEP_2_FIELD_MAX_COMPLETION_TOKENS = 6144
# 当前抽取不使用工具调用；请求级屏蔽协议标记，避免模型把结构化 JSON 错误收尾为工具块。
DISALLOWED_TOOL_PROTOCOL_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<tool_response>",
    "</tool_response>",
)
class StrictModel(BaseModel):
    """所有正式模型响应都禁止额外字段，确保协议稳定。"""

    model_config = ConfigDict(extra="forbid")


class StructuredOutputError(RuntimeError):
    """保留结构化输出失败的结束原因，供上层决定是否降级重试。"""

    def __init__(
        self,
        message: str,
        *,
        finish_reason: str | None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason
        self.metrics = metrics or {}


class PartyHint(StrictModel):
    name: str | None = None
    source_designation: str | None = Field(
        default=None, max_length=40, description="合同原文中的主体称谓，不得归一化或改写"
    )
    evidence: str | None = Field(default=None, max_length=120)
    page: int | None = Field(default=None, ge=1)
    certainty: Literal["明确", "可能", "未发现"]


class PageMapItem(StrictModel):
    page: int = Field(ge=1)
    section_or_topic: str
    summary: str = Field(max_length=300)
    quality_notes: list[str] = Field(default_factory=list, max_length=5)


class InformationLocation(StrictModel):
    topic: str
    likely_pages: list[int] = Field(default_factory=list)
    section_hint: str | None = None
    evidence: str | None = Field(default=None, max_length=160)
    certainty: Literal["明确", "可能", "未发现"]


class AmountFeeMention(StrictModel):
    """Step 1 逐条保存金额、税费及其他费用原文，不提前合并计价口径。"""

    page: int = Field(ge=1, description="原文所在物理 PDF 页码")
    category: Literal[
        "contract_total",
        "tax_inclusive_amount",
        "tax_exclusive_price",
        "unit_price",
        "tax_rate",
        "tax_or_invoice",
        "freight",
        "insurance",
        "service_fee",
        "other_fee",
        "payment",
        "deposit",
        "penalty",
        "settlement",
        "other",
    ]
    source_text: str = Field(
        max_length=320, description="图像中可见的金额或费用相关最小完整原文"
    )
    scope_or_context: str | None = Field(
        default=None,
        max_length=160,
        description="原文明确对应的产品、总价、付款阶段或费用范围；不作计算",
    )


class ContractOverview(StrictModel):
    is_contract: bool
    contract_type_guess: str | None = None
    language: str | None = None
    page_count: int | None = Field(default=None, ge=1)
    summary: str = Field(max_length=240)


class ContractUnderstanding(StrictModel):
    document_overview: ContractOverview
    # 仅建立主体定位线索，保留原始称谓，避免第一步提前制造甲乙方映射。
    parties_hint: list[PartyHint] = Field(default_factory=list, max_length=8)
    page_map: list[PageMapItem] = Field(default_factory=list, max_length=5)
    information_locations: list[InformationLocation] = Field(default_factory=list, max_length=10)
    amount_and_fee_mentions: list[AmountFeeMention] = Field(
        description="可见页面中全部金额、税率、税费和价外费用相关原文，按页码和出现顺序保存",
    )
    risks_and_conflicts: list[str] = Field(default_factory=list, max_length=5)
    unresolved_items: list[str] = Field(default_factory=list, max_length=5)


class CoreExtraction(StrictModel):
    """应用层汇总结果；对象字段已经补入确定性外层状态。"""

    document_id: str = Field(
        pattern=SHA256_DOCUMENT_ID_PATTERN,
        description="程序根据原始 PDF 文件字节计算的 SHA-256",
    )
    fields: dict[str, CoreFieldValue | ObjectFieldValue] = Field(
        description="以 field_id 为键的全部 Core 字段结果"
    )


def _read_text_sync(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


_BULLET_LABELS = {
    "document_overview": "文档概览",
    "is_contract": "是否为合同",
    "contract_type_guess": "文书性质猜测",
    "language": "语言",
    "page_count": "原始 PDF 物理页数",
    "summary": "摘要",
    "parties_hint": "主体线索",
    "name": "主体原文名称",
    "source_designation": "原文称谓",
    "evidence": "原文证据",
    "page": "物理页码",
    "certainty": "确定性",
    "page_map": "页面地图",
    "section_or_topic": "章节或主题",
    "quality_notes": "质量说明",
    "information_locations": "关键信息定位",
    "topic": "主题",
    "likely_pages": "可能页码",
    "section_hint": "位置提示",
    "amount_and_fee_mentions": "金额与费用原文清单",
    "category": "类别",
    "source_text": "原文",
    "scope_or_context": "范围或上下文",
    "risks_and_conflicts": "风险与冲突",
    "unresolved_items": "未确定事项",
}


def _bullet_scalar(value: Any) -> str:
    """将 JSON 标量渲染为稳定单行文本，避免 bullet 上下文重新变成 JSON。"""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).replace("\n", " / ")


def _render_bullet_node(value: Any, *, label: str | None, indent: int) -> list[str]:
    """递归渲染带缩进的条目，保持 Pydantic 字段与列表顺序。"""

    prefix = "  " * indent + "- "
    if isinstance(value, dict):
        lines = [] if label is None else [f"{prefix}{label}："]
        child_indent = indent if label is None else indent + 1
        for key, child in value.items():
            lines.extend(
                _render_bullet_node(
                    child,
                    label=_BULLET_LABELS.get(key, key),
                    indent=child_indent,
                )
            )
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}{label}：（无）"]
        lines = [f"{prefix}{label}："]
        for index, item in enumerate(value, start=1):
            item_prefix = "  " * (indent + 1) + "- "
            if isinstance(item, dict):
                lines.append(f"{item_prefix}条目 {index}：")
                for key, child in item.items():
                    lines.extend(
                        _render_bullet_node(
                            child,
                            label=_BULLET_LABELS.get(key, key),
                            indent=indent + 2,
                        )
                    )
            else:
                lines.append(f"{item_prefix}{_bullet_scalar(item)}")
        return lines
    return [f"{prefix}{label}：{_bullet_scalar(value)}"]


def render_contract_understanding_bullets(
    understanding: ContractUnderstanding,
) -> str:
    """把 Step 1 校验后 JSON 等价转换为供 Step 2 阅读的 bullet 上下文。"""

    return "\n".join(
        _render_bullet_node(
            understanding.model_dump(mode="json"),
            label=None,
            indent=0,
        )
    )


def _load_settings_sync(root: Path) -> dict[str, Any]:
    load_dotenv(root / ".env")
    with (root / "configs/settings.yaml").open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def response_format(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """将指定 JSON Schema 交给 vLLM 的 OpenAI 兼容接口进行约束生成。"""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def normalize_effective_date_provenance(
    field_id: str,
    field: CoreFieldValue | ObjectFieldCandidate,
) -> CoreFieldValue | ObjectFieldCandidate:
    """移除没有生效条款原文支撑的条件完成日期。

    签订日期可能与签字盖章条款同时出现，但两者并不自动构成同一天完成条件的证据。
    对非 ``explicit_date`` 机制，仅当日期原文也出现在生效条款证据中时才保留日期；
    原始模型响应仍单独落盘，归一化结果保留被排除的日期原文以便审计。
    """

    if field_id != "effective_mechanism" or not isinstance(
        field, ObjectFieldCandidate
    ):
        return field
    date_property = field.properties.get("date")
    trigger_type = field.properties.get("trigger_type")
    trigger_text = field.properties.get("trigger_text")
    if (
        date_property is None
        or date_property.status != "found"
        or trigger_type is None
        or trigger_text is None
        or effective_date_has_provenance(
            date_raw_value=date_property.raw_value,
            trigger_type=trigger_type.value,
            trigger_text=trigger_text.raw_value,
        )
    ):
        return field

    properties = dict(field.properties)
    properties["date"] = date_property.model_copy(
        update={
            "reason": "生效条款只明示触发机制，未给出可核对的条件完成日期。",
            "status": "not_found",
            "value": None,
        }
    )
    return field.model_copy(update={"properties": properties})


def validate_required_fields(
    fields: dict[str, CoreFieldValue | ObjectFieldValue],
    required_field_ids: set[str],
) -> list[dict[str, Any]]:
    """校验合同级必填字段；缺失或非 found 结果不得进入最终产物。"""

    violations: list[dict[str, Any]] = []
    for field_id in sorted(required_field_ids):
        field = fields.get(field_id)
        if field is None:
            violations.append({"field_id": field_id, "reason": "必填字段未成功提取"})
            continue
        if field.status != "found":
            violations.append(
                {
                    "field_id": field_id,
                    "reason": f"必填字段状态必须为 found，实际为 {field.status}",
                }
            )
            continue
        if isinstance(field, CoreFieldValue) and (
            field.value is None
            or (isinstance(field.value, str) and not field.value.strip())
        ):
            violations.append({"field_id": field_id, "reason": "必填字段 value 不得为空"})
    return violations


def messages_for(
    prompt: str,
    images: list[dict[str, Any]],
    prompt_suffix: str | None = None,
) -> list[dict[str, Any]]:
    """构造多模态消息；共享文本和图像在前，各步骤任务全部置于图像后。"""

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image["data_url"]}} for image in images
    )
    if prompt_suffix:
        content.append({"type": "text", "text": prompt_suffix})
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": content},
    ]


def build_sampling_parameters(generation: dict[str, Any]) -> dict[str, Any]:
    """将模型采样配置映射为 OpenAI 兼容参数及 vLLM 扩展参数。"""
    return {
        "temperature": float(generation["temperature"]),
        "top_p": float(generation["top_p"]),
        "presence_penalty": float(generation["presence_penalty"]),
        "seed": int(generation["seed"]),
        # 这些参数不是 OpenAI 标准字段，通过 vLLM 扩展体传递。
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
    prompt: str,
    images: list[dict[str, Any]],
    schema_model: type[BaseModel],
    schema_name: str,
    json_schema: dict[str, Any] | None = None,
    generation: dict[str, Any],
    max_completion_tokens: int,
    prompt_suffix: str | None = None,
    model_request_limiter: ModelRequestLimiter,
) -> tuple[BaseModel, dict[str, Any], dict[str, Any]]:
    effective_schema = json_schema or schema_model.model_json_schema()
    started_at = time.perf_counter()
    async with model_request_limiter.slot():
        completion = await client.chat.completions.create(
            model=model,
            messages=messages_for(prompt, images, prompt_suffix),
            response_format=response_format(effective_schema, schema_name),
            max_completion_tokens=max_completion_tokens,
            **build_sampling_parameters(generation),
        )
    elapsed_seconds = round(time.perf_counter() - started_at, 3)
    usage = completion.usage.model_dump() if completion.usage else {}
    metrics = {
        "elapsed_seconds": elapsed_seconds,
        "usage": usage,
        "prompt_text_characters": len(prompt) + len(prompt_suffix or ""),
        "image_count": len(images),
        "image_bytes": sum(image["image_bytes"] for image in images),
    }
    raw = completion.model_dump(mode="json")
    content = completion.choices[0].message.content
    if not content:
        raise StructuredOutputError(
            "模型未返回 JSON 内容。请检查 vLLM 的 structured outputs 配置与服务日志。",
            finish_reason=completion.choices[0].finish_reason,
        )
    try:
        decoded = json.loads(content)
        # 模型服务可能回退到非约束生成；本地必须使用同一 Schema 再验证一次。
        Draft202012Validator(effective_schema).validate(decoded)
        parsed = schema_model.model_validate(decoded)
    except Exception as error:
        finish_reason = completion.choices[0].finish_reason
        raise StructuredOutputError(
            "模型响应未通过 JSON Schema 校验。"
            f"finish_reason={finish_reason!r}，请优先检查是否为长度截断。",
            finish_reason=finish_reason,
            metrics=metrics,
        ) from error
    return parsed, metrics, raw


async def run_core_extraction(
    *,
    project_root_path: Path,
    pdf_path: Path,
    document_id: str,
    shared_images: list[dict[str, Any]],
    shared_source_page_count: int,
    shared_client: AsyncOpenAI,
    model_request_limiter: ModelRequestLimiter,
    core_catalog_path: Path | None = None,
    attribute_catalog_path: Path | None = None,
) -> StageResult[dict[str, Any]]:
    """异步执行 Core 算法，所有结果和校验仅在内存中传递。"""

    root, pdf_path = await asyncio.gather(
        run_blocking(project_root_path.resolve),
        run_blocking(pdf_path.resolve),
    )
    settings = await run_blocking(_load_settings_sync, root)
    mllm = settings["models"]["mllm"]
    if not await run_blocking(pdf_path.is_file):
        raise FileNotFoundError(f"找不到待分析 PDF：{pdf_path}")
    images = shared_images
    source_page_count = shared_source_page_count
    prompt_dir = Path(__file__).parent / "prompts"
    common_prefix = await build_common_prefix(source_page_count)
    understanding_prompt = (
        await run_blocking(_read_text_sync, prompt_dir / "01_understand_contract.txt")
    ).replace("{{PAGE_VISIBILITY_CONTEXT}}", "")
    # 生产沿用默认目录；Discovery 实验可显式注入隔离目录以复用同一算法。
    core_yaml_path = core_catalog_path or root / settings["paths"]["core_fields"]
    if not core_yaml_path.is_absolute():
        core_yaml_path = root / core_yaml_path
    effective_attribute_catalog_path = (
        attribute_catalog_path or root / settings["paths"]["attribute_fields"]
    )
    if not effective_attribute_catalog_path.is_absolute():
        effective_attribute_catalog_path = root / effective_attribute_catalog_path
    core_yaml = await run_blocking(_read_text_sync, core_yaml_path)
    # 提前解析 YAML，避免模型已调用后才发现字段定义文件无效。
    core_payload = await run_blocking(yaml.safe_load, core_yaml)
    if not isinstance(core_payload, dict) or not isinstance(
        core_payload.get("fields"), list
    ):
        raise RuntimeError("Core 字段目录根节点必须包含 fields 数组。")
    core_fields = core_payload["fields"]
    if not core_fields:
        raise RuntimeError(
            "活动 Core 提取服务不接受空字段目录；"
            "discovery 模式应由 EmptyCoreExtractionService 处理 0 Core。"
        )
    field_catalog = YamlFieldCatalog(
        core_path=core_yaml_path,
        attribute_path=effective_attribute_catalog_path,
    )
    core_field_definitions = list(await field_catalog.load(FieldKind.CORE))
    definitions_by_id = {field.field_id: field for field in core_field_definitions}
    expected_field_ids = {field["field_id"] for field in core_fields}
    required_field_ids = {
        field["field_id"]
        for field in core_fields
        if field.get("output", {}).get("nullable") is False
    }
    if set(definitions_by_id) != expected_field_ids:
        raise ValueError("字段目录对象与 Core YAML 的 field_id 不一致。")
    client = shared_client
    generation = mllm["generation"]
    max_model_len = mllm["context_window_tokens"]
    if max_model_len < 1:
        raise ValueError("上下文长度必须大于 0。")

    understanding, step1_metrics, _ = await invoke_json(
        client=client,
        model=mllm["model"],
        prompt=common_prefix,
        prompt_suffix=understanding_prompt,
        images=images,
        schema_model=ContractUnderstanding,
        schema_name="contract_understanding",
        generation=generation,
        max_completion_tokens=min(
            generation["max_completion_tokens"], STEP_1_MAX_COMPLETION_TOKENS
        ),
        model_request_limiter=model_request_limiter,
    )
    understanding_bullets = render_contract_understanding_bullets(understanding)

    extraction_template = await run_blocking(
        _read_text_sync, prompt_dir / "02_extract_core.txt"
    )
    marker = "{{CORE_FIELDS_YAML}}"
    if extraction_template.count(marker) != 1:
        raise ValueError("第二步提示词必须恰好包含一个 CORE_FIELDS_YAML 占位符。")
    understanding_marker = "{{CONTRACT_UNDERSTANDING_BULLETS}}"
    if extraction_template.count(understanding_marker) != 1:
        raise ValueError(
            "第二步提示词必须恰好包含一个 CONTRACT_UNDERSTANDING_BULLETS 占位符。"
        )
    extraction_prefix_template, extraction_suffix_template = extraction_template.split(marker)
    extraction_common_prompt = (
        extraction_prefix_template.replace(
            understanding_marker, understanding_bullets
        ).replace("{{PAGE_VISIBILITY_CONTEXT}}", "")
    )
    merged_fields: dict[str, CoreFieldValue | ObjectFieldValue] = {}
    field_records: list[dict[str, Any]] = []

    # 每次调用只生成一个 Core 字段，使标量 reason 或对象子字段 reasons 与判断对象直接绑定。
    # 字段失败只隔离当前字段并继续后续字段，不再创建语义相同的拆分重试。
    for field_index, field in enumerate(core_fields, start=1):
        field_id = field["field_id"]
        attempt_id = f"{field_index:02d}"
        field_schema = build_core_extraction_schema([field])
        field_prompt_suffix = (
            build_compact_field_prompt([definitions_by_id[field_id]])
            + extraction_suffix_template
        )
        normalization_applied = False
        try:
            field_candidate, field_metrics, field_raw_response = await invoke_json(
                client=client,
                model=mllm["model"],
                prompt=common_prefix,
                prompt_suffix=extraction_common_prompt + field_prompt_suffix,
                images=images,
                schema_model=CoreExtractionCandidate,
                schema_name=f"core_field_extraction_{attempt_id}_{field_id}",
                json_schema=field_schema,
                generation=generation,
                max_completion_tokens=min(
                    generation["max_completion_tokens"],
                    STEP_2_FIELD_MAX_COMPLETION_TOKENS,
                ),
                model_request_limiter=model_request_limiter,
            )
            normalized_fields = {
                extracted_field_id: normalize_effective_date_provenance(
                    extracted_field_id, extracted_field
                )
                for extracted_field_id, extracted_field in field_candidate.fields.items()
            }
            normalization_applied = any(
                normalized_fields[extracted_field_id] != extracted_field
                for extracted_field_id, extracted_field in field_candidate.fields.items()
            )
            field_candidate = field_candidate.model_copy(
                update={"fields": normalized_fields}
            )
            envelope_errors = {
                extracted_field_id: errors
                for extracted_field_id, extracted_field in field_candidate.fields.items()
                if (
                    errors := validate_extracted_field(
                        extracted_field_id, extracted_field
                    )
                )
            }
            if envelope_errors:
                finish_reason = field_raw_response["choices"][0]["finish_reason"]
                rendered_errors = "；".join(
                    f"{invalid_field_id}: {', '.join(errors)}"
                    for invalid_field_id, errors in envelope_errors.items()
                )
                raise StructuredOutputError(
                    f"模型响应未通过业务包络校验：{rendered_errors}",
                    finish_reason=finish_reason,
                    metrics=field_metrics,
                )
        except StructuredOutputError as error:
            # 仅隔离当前字段的模型输出失败；连接、鉴权等基础设施错误仍立即终止。
            field_metrics = error.metrics
            failure = {
                "field_index": field_index,
                "attempt_id": attempt_id,
                "field_id": field_id,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "finish_reason": error.finish_reason,
                "metrics": field_metrics,
            }
            field_records.append(failure)
            continue

        actual_ids = set(field_candidate.fields)
        if actual_ids != {field_id}:
            raise RuntimeError(
                f"字段 {field_id} 的响应覆盖异常："
                f"缺少 {sorted({field_id} - actual_ids)}，"
                f"额外 {sorted(actual_ids - {field_id})}"
            )
        duplicate_ids = set(merged_fields) & actual_ids
        if duplicate_ids:
            raise RuntimeError(f"逐字段提取出现重复字段：{sorted(duplicate_ids)}")

        finalized_field = finalize_candidate_field(field_candidate.fields[field_id])
        merged_fields[field_id] = finalized_field
        record = {
            "field_index": field_index,
            "attempt_id": attempt_id,
            "field_id": field_id,
            "status": "succeeded",
            "document_id": document_id,
            "reason": (
                finalized_field.reason
                if isinstance(finalized_field, CoreFieldValue)
                else None
            ),
            "property_reasons": (
                {
                    property_name: property_value.reason
                    for property_name, property_value in finalized_field.properties.items()
                }
                if isinstance(finalized_field, ObjectFieldValue)
                else None
            ),
            "derived_object_status": (
                finalized_field.status
                if isinstance(finalized_field, ObjectFieldValue)
                else None
            ),
            "semantic_normalization_applied": normalization_applied,
            "metrics": field_metrics,
        }
        field_records.append(record)

    step2_metrics = aggregate_field_metrics(field_records)
    step2_metrics["execution_mode"] = "single_field"
    step2_metrics["unresolved_field_ids"] = sorted(expected_field_ids - set(merged_fields))
    if not merged_fields:
        raise RuntimeError("所有 Core 字段提取均失败，无法生成合并结果。")
    extraction = CoreExtraction(
        document_id=document_id,
        fields=merged_fields,
    )
    extracted_ids = set(extraction.fields)
    missing_ids = sorted(expected_field_ids - extracted_ids)
    unexpected_ids = sorted(extracted_ids - expected_field_ids)
    invalid_envelopes = [
        {"field_id": field_id, "errors": errors}
        for field_id, field in extraction.fields.items()
        if (errors := validate_extracted_field(field_id, field))
    ]
    required_field_violations = validate_required_fields(
        extraction.fields, required_field_ids
    )
    validation = {
        "missing_field_ids": missing_ids,
        "unexpected_field_ids": unexpected_ids,
        "invalid_field_envelopes": invalid_envelopes,
        "required_field_violations": required_field_violations,
    }
    metrics = {
        "max_model_len": max_model_len,
        "step_1": step1_metrics,
        "step_2": step2_metrics,
    }
    if required_field_violations:
        raise RuntimeError(
            "必填 Core 字段校验失败："
            + "；".join(
                f"{item['field_id']}: {item['reason']}"
                for item in required_field_violations
            )
        )
    # document_id 来自原始文件字节；业务字段为空不会改变或阻断文档身份。
    return StageResult(
        payload=extraction.model_dump(mode="json"),
        validation=validation,
        metrics=metrics,
        artifacts={
            # 这是供同一合同的固定 Attribute 阶段复用的定位辅助，不属于 Core 对外业务结果。
            "contract_understanding_bullets": understanding_bullets,
        },
    )
