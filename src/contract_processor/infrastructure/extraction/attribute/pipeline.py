"""按固定字段目录逐项提取生产 Attribute，并复用 Core 的合同理解地图。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

try:
    from contract_processor.application.prompts.core_fields import build_compact_field_prompt
    from contract_processor.application.prompts.pdf_prefix import (
        SYSTEM_MESSAGE,
        build_common_prefix,
    )
    from contract_processor.application.schemas.core_extraction import (
        build_field_extraction_schema,
    )
    from contract_processor.async_utils import run_blocking
    from contract_processor.domain.enums import FieldKind
    from contract_processor.domain.models import FieldDefinition
    from contract_processor.infrastructure.extraction.field_values import (
        FieldExtractionCandidate,
        FinalFieldValue,
        ObjectFieldValue,
        ScalarFieldValue,
        aggregate_field_metrics,
        finalize_candidate_field,
        validate_extracted_field,
    )
    from contract_processor.infrastructure.extraction.stage_result import StageResult
    from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
    from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
    from dotenv import load_dotenv
    from jsonschema import Draft202012Validator
    from openai import AsyncOpenAI
    from pydantic import BaseModel
    import yaml
except ImportError as error:  # 依赖在模型调用前检查，避免产生部分 Attribute 结果。
    raise RuntimeError(
        "缺少 Attribute 抽取依赖。请在已激活的环境执行：\n"
        "python -m pip install -e .\n"
        f"原始错误：{error}"
    ) from error


FIELD_MAX_COMPLETION_TOKENS = 6144
DISALLOWED_TOOL_PROTOCOL_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<tool_response>",
    "</tool_response>",
)
PROJECT_IDENTIFIER_LABEL_PATTERN = re.compile(
    r"(?:项目编号|项目号|项目编码|项目代码|招标编号|采购项目编号|工程编号|立项编号|任务编号)"
)
PAYMENT_DEADLINE_PATTERN = re.compile(
    r"(?:\d+|[一二三四五六七八九十百千万两]+)\s*(?:个)?(?:工作日|日|天|月|年)(?:内|前|后|之内)?"
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?:前|前后|起)?"
    r"|(?:不晚于|截止|届满|交付前|验收前|发票开具前)"
)
PAYMENT_ONLY_PATTERN = re.compile(r"(?:支付|付款|价款|款项|预付款|尾款|验收款)")
ACCEPTANCE_PROCEDURE_PATTERN = re.compile(
    r"(?:验收标准|验收合格|验收通过|验收条件|验收报告|验收单|验收完成|验收时限|验收期限"
    r"|提出.{0,8}异议|(?:收货|到货|安装|提交).{0,20}验收)"
)
RELATIONAL_INSTITUTION_PATTERN = re.compile(
    r"(?:(?:甲方|乙方|买方|卖方|双方).{0,8}(?:所在地|当地)|(?:所在地|当地|有管辖权))"
    r".{0,10}(?:人民法院|法院)"
)
WARRANTY_START_PATTERN = re.compile(r"(?:自|从).{0,40}(?:起|之日起)|(?:验收合格|交付|安装).{0,20}后")


class StructuredOutputError(RuntimeError):
    """保留结构化失败信息，以支持单字段隔离和最终阶段门禁。"""

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


def _read_text_sync(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _load_settings_sync(root: Path) -> dict[str, Any]:
    load_dotenv(root / ".env")
    with (root / "configs/settings.yaml").open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def response_format(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def build_sampling_parameters(generation: dict[str, Any]) -> dict[str, Any]:
    """保持与 Core 一致的 vLLM 采样和工具协议屏蔽策略。"""

    return {
        "temperature": float(generation["temperature"]),
        "top_p": float(generation["top_p"]),
        "presence_penalty": float(generation["presence_penalty"]),
        "seed": int(generation["seed"]),
        "extra_body": {
            "top_k": int(generation["top_k"]),
            "repetition_penalty": float(generation["repetition_penalty"]),
            "bad_words": list(DISALLOWED_TOOL_PROTOCOL_MARKERS),
        },
    }


def messages_for(
    prompt: str,
    images: list[dict[str, Any]],
    prompt_suffix: str,
) -> list[dict[str, Any]]:
    """任务说明位于图像之后，确保共享多模态前缀可被 vLLM 复用。"""

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image["data_url"]}}
        for image in images
    )
    content.append({"type": "text", "text": prompt_suffix})
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": content},
    ]


async def invoke_json(
    *,
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    images: list[dict[str, Any]],
    prompt_suffix: str,
    schema: dict[str, Any],
    schema_name: str,
    generation: dict[str, Any],
    model_request_limiter: ModelRequestLimiter,
) -> tuple[FieldExtractionCandidate, dict[str, Any]]:
    """在共享请求门禁内请求并二次校验单字段 JSON。"""

    started_at = time.perf_counter()
    async with model_request_limiter.slot():
        completion = await client.chat.completions.create(
            model=model,
            messages=messages_for(prompt, images, prompt_suffix),
            response_format=response_format(schema, schema_name),
            max_completion_tokens=min(
                int(generation["max_completion_tokens"]), FIELD_MAX_COMPLETION_TOKENS
            ),
            **build_sampling_parameters(generation),
        )
    metrics = {
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "usage": completion.usage.model_dump() if completion.usage else {},
        "prompt_text_characters": len(prompt) + len(prompt_suffix),
        "image_count": len(images),
        "image_bytes": sum(image["image_bytes"] for image in images),
    }
    content = completion.choices[0].message.content
    if not content:
        raise StructuredOutputError(
            "模型未返回 JSON 内容。请检查 vLLM 的 structured outputs 配置与服务日志。",
            finish_reason=completion.choices[0].finish_reason,
            metrics=metrics,
        )
    try:
        decoded = json.loads(content)
        Draft202012Validator(schema).validate(decoded)
        return FieldExtractionCandidate.model_validate(decoded), metrics
    except Exception as error:
        raise StructuredOutputError(
            "模型响应未通过 Attribute JSON Schema 校验。"
            f"finish_reason={completion.choices[0].finish_reason!r}，请优先检查是否为长度截断。",
            finish_reason=completion.choices[0].finish_reason,
            metrics=metrics,
        ) from error


def _accepted_core_value(value: Any) -> Any | None:
    """只保留已成功提取的规范值，避免把 Core 的审计包络注入 Attribute 提示词。"""

    if not isinstance(value, dict) or value.get("status") != "found":
        return None
    if "properties" not in value:
        normalized = value.get("value")
        return normalized if normalized not in (None, "", [], {}) else None
    properties = value["properties"]
    if not isinstance(properties, dict):
        return None
    compact = {
        key: property_value.get("value")
        for key, property_value in properties.items()
        if isinstance(property_value, dict)
        and property_value.get("status") == "found"
        and property_value.get("value") not in (None, "", [], {})
    }
    return compact or None


def render_compact_core_context(
    core_fields: dict[str, Any], core_definitions: list[FieldDefinition]
) -> str:
    """按目录顺序渲染成功 Core 值，作为简洁辅助上下文而非第二份事实源。"""

    lines = ["仅列出 status=found 且规范值非空的 Core 结果："]
    for definition in core_definitions:
        value = _accepted_core_value(core_fields.get(definition.field_id))
        if value is None:
            continue
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"- {definition.name}（{definition.field_id}）：{rendered}")
    if len(lines) == 1:
        lines.append("- 无可用 Core 结果；请仅依据 PDF 图像与字段定义判断。")
    return "\n".join(lines)


def build_attribute_field_prompt(definition: FieldDefinition) -> str:
    """以统一字段定义生成当前字段的语义判别清单和有限正例。"""

    examples = "\n".join(
        "- 原文示例："
        + example.source_text.replace("\n", " / ")
        + "\n  合法规范值示例："
        + json.dumps(example.output, ensure_ascii=False, separators=(",", ":"))
        for example in definition.examples[:2]
    )
    checklist = [
        "【当前字段判别清单】",
        "- not_meaning 列出的概念是硬排除：即使词面相近，也不得作为 found 的证据或 value。",
        "- 每个 raw_value 必须直接支撑当前字段或当前对象子字段；同段落中的相邻义务、金额或期限不能迁移采用。",
    ]
    if examples:
        checklist.extend(
            [
                "- 以下仅用于理解字段边界，不是当前合同事实，也不得复制其中的值：",
                examples,
            ]
        )
    return build_compact_field_prompt([definition]) + "\n" + "\n".join(checklist)


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _property_value(field: ObjectFieldValue, name: str) -> Any | None:
    return field.properties.get(name)


def validate_attribute_business_rules(
    field_id: str, field: FinalFieldValue
) -> list[str]:
    """校验跨合同稳定的字段语义不变量，不按单一合同的字面措辞裁决。"""

    errors: list[str] = []
    if isinstance(field, ScalarFieldValue):
        if field.status == "found" and _nonempty_string(field.raw_value) is None:
            errors.append("found 状态必须保留直接支撑该字段的 raw_value")
    else:
        for property_name, property_value in field.properties.items():
            if (
                property_value.status == "found"
                and _nonempty_string(property_value.raw_value) is None
            ):
                errors.append(
                    f"{property_name}: found 状态必须保留直接支撑该子字段的 raw_value"
                )

    if field_id == "project_numbers" and isinstance(field, ScalarFieldValue):
        if field.status == "found":
            raw_value = _nonempty_string(field.raw_value) or ""
            if not PROJECT_IDENTIFIER_LABEL_PATTERN.search(raw_value):
                errors.append(
                    "project_numbers: found 的 raw_value 必须包含明确项目编号语义的标签，"
                    "项目名称或其中的编码式片段不能单独采用"
                )

    if field_id == "payment_schedule" and isinstance(field, ScalarFieldValue):
        if field.status == "found" and isinstance(field.value, list):
            for index, item in enumerate(field.value, start=1):
                if not isinstance(item, dict):
                    continue
                trigger = _nonempty_string(item.get("trigger_text"))
                due = _nonempty_string(item.get("due_text"))
                if trigger and due and trigger == due:
                    errors.append(
                        f"payment_schedule[{index}]: trigger_text 与 due_text 不能复制为同一文本，"
                        "应拆分付款事件与付款期限"
                    )
                if due and not PAYMENT_DEADLINE_PATTERN.search(due):
                    errors.append(
                        f"payment_schedule[{index}]: due_text 必须是付款期限或确定付款日，"
                        "不能只填写触发条件"
                    )

    if field_id == "acceptance_mechanism" and isinstance(field, ObjectFieldValue):
        deadline = _property_value(field, "deadline_text")
        if deadline is not None and deadline.status == "found":
            raw_value = _nonempty_string(deadline.raw_value) or ""
            if (
                PAYMENT_ONLY_PATTERN.search(raw_value)
                and not ACCEPTANCE_PROCEDURE_PATTERN.search(raw_value)
            ):
                errors.append(
                    "acceptance_mechanism.deadline_text: 仅包含付款义务或验收款支付期限，"
                    "未体现验收行为、标准、异议或验收期限，不能作为验收期限"
                )

    if field_id == "dispute_resolution" and isinstance(field, ObjectFieldValue):
        institution = _property_value(field, "institution_name")
        if institution is not None and institution.status == "found":
            raw_value = _nonempty_string(institution.raw_value) or ""
            if RELATIONAL_INSTITUTION_PATTERN.search(raw_value):
                errors.append(
                    "dispute_resolution.institution_name: 关系性地域法院描述不能作为具体机构名称，"
                    "应为 null 并保留在 jurisdiction_text"
                )

    if field_id == "warranty_commitment" and isinstance(field, ObjectFieldValue):
        start_trigger = _property_value(field, "start_trigger_text")
        if start_trigger is not None and start_trigger.status == "found":
            raw_value = _nonempty_string(start_trigger.raw_value) or ""
            if not WARRANTY_START_PATTERN.search(raw_value):
                errors.append(
                    "warranty_commitment.start_trigger_text: raw_value 未表达质保期间的起算事件或条件"
                )
    return errors


def render_retry_feedback(errors: list[str]) -> str:
    """只向模型提供可执行校验指导，不回传错误候选以避免事实锚定。"""

    return "\n".join(f"- {error}" for error in errors)


def aggregate_attempt_metrics(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """把局部重试开销纳入字段指标，避免成功末次掩盖前序失败。"""

    return {
        "elapsed_seconds": round(
            sum(float(metrics.get("elapsed_seconds") or 0) for metrics in attempts), 3
        ),
        "usage": {
            key: sum(
                int(metrics.get("usage", {}).get(key) or 0) for metrics in attempts
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def _final_attribute_item(field_id: str, field: FinalFieldValue) -> dict[str, Any]:
    """对外 Attribute 保持有序列表，每项显式携带固定 field_id。"""

    return {"field_id": field_id, **field.model_dump(mode="json")}


async def run_attribute_extraction(
    *,
    project_root_path: Path,
    document_id: str,
    shared_images: list[dict[str, Any]],
    shared_source_page_count: int,
    shared_client: AsyncOpenAI,
    model_request_limiter: ModelRequestLimiter,
    core_fields: dict[str, Any],
    contract_understanding_bullets: str,
    core_catalog_path: Path | None = None,
    attribute_catalog_path: Path | None = None,
) -> StageResult[list[dict[str, Any]]]:
    """逐字段执行正式 Attribute 提取，所有结果和中间信息只在内存中传递。"""

    root = await run_blocking(project_root_path.resolve)
    settings = await run_blocking(_load_settings_sync, root)
    mllm = settings["models"]["mllm"]
    if int(mllm["context_window_tokens"]) < 1:
        raise ValueError("上下文长度必须大于 0。")
    if not contract_understanding_bullets.strip():
        raise RuntimeError("Attribute 提取必须接收 Core 产生的合同理解地图。")

    # 生产沿用默认目录；Discovery 实验可显式注入隔离目录以复用同一算法。
    attribute_yaml_path = (
        attribute_catalog_path or root / settings["paths"]["attribute_fields"]
    )
    if not attribute_yaml_path.is_absolute():
        attribute_yaml_path = root / attribute_yaml_path
    effective_core_catalog_path = (
        core_catalog_path or root / settings["paths"]["core_fields"]
    )
    if not effective_core_catalog_path.is_absolute():
        effective_core_catalog_path = root / effective_core_catalog_path
    attribute_yaml = await run_blocking(_read_text_sync, attribute_yaml_path)
    attribute_payload = await run_blocking(yaml.safe_load, attribute_yaml)
    if not isinstance(attribute_payload, dict) or not isinstance(
        attribute_payload.get("fields"), list
    ):
        raise RuntimeError("Attribute 字段目录根节点必须包含 fields 数组。")
    raw_fields = attribute_payload["fields"]
    if attribute_payload.get("status") == "empty" or not raw_fields:
        raise RuntimeError("活动 Attribute 提取服务不能处理空目录。")
    extraction_policy = attribute_payload.get("extraction", {})
    if not isinstance(extraction_policy, dict):
        raise RuntimeError("Attribute extraction 策略必须是对象。")
    max_retries_per_field = extraction_policy.get("max_retries_per_field", 0)
    if not isinstance(max_retries_per_field, int) or max_retries_per_field < 0:
        raise ValueError("Attribute max_retries_per_field 必须是非负整数。")

    catalog = YamlFieldCatalog(
        core_path=effective_core_catalog_path,
        attribute_path=attribute_yaml_path,
    )
    core_definitions, attribute_definitions = await asyncio.gather(
        catalog.load(FieldKind.CORE), catalog.load(FieldKind.ATTRIBUTE)
    )
    definitions_by_id = {definition.field_id: definition for definition in attribute_definitions}
    expected_field_ids = [str(field["field_id"]) for field in raw_fields]
    if set(definitions_by_id) != set(expected_field_ids):
        raise ValueError("字段目录对象与 Attribute YAML 的 field_id 不一致。")
    if len(expected_field_ids) != len(set(expected_field_ids)):
        raise ValueError("Attribute YAML 包含重复 field_id。")

    prompt_dir = Path(__file__).parent / "prompts"
    template = await run_blocking(_read_text_sync, prompt_dir / "01_extract_attribute_field.txt")
    markers = {
        "{{ATTRIBUTE_FIELDS_YAML}}": 1,
        "{{CONTRACT_UNDERSTANDING_BULLETS}}": 1,
        "{{COMPACT_CORE_CONTEXT}}": 1,
    }
    if any(template.count(marker) != count for marker, count in markers.items()):
        raise ValueError("Attribute 提示词必须各包含一次字段、理解地图和 Core 上下文占位符。")
    common_prefix = await build_common_prefix(shared_source_page_count)
    stable_prompt = (
        template.replace("{{PAGE_VISIBILITY_CONTEXT}}", "")
        .replace("{{CONTRACT_UNDERSTANDING_BULLETS}}", contract_understanding_bullets)
        .replace(
            "{{COMPACT_CORE_CONTEXT}}",
            render_compact_core_context(core_fields, list(core_definitions)),
        )
    )
    before_field, after_field = stable_prompt.split("{{ATTRIBUTE_FIELDS_YAML}}")
    merged_fields: dict[str, FinalFieldValue] = {}
    field_records: list[dict[str, Any]] = []

    # 保持与 Core 一致的逐字段隔离：当前 Attribute 失败不影响其余固定字段继续完成。
    for field_index, field in enumerate(raw_fields, start=1):
        field_id = str(field["field_id"])
        field_schema = build_field_extraction_schema([field], field_set_name="Attribute")
        base_prompt_suffix = (
            before_field
            + build_attribute_field_prompt(definitions_by_id[field_id])
            + after_field
        )
        attempt_metrics: list[dict[str, Any]] = []
        last_error: StructuredOutputError | None = None
        finalized: FinalFieldValue | None = None
        for attempt in range(1, max_retries_per_field + 2):
            retry_suffix = ""
            if last_error is not None:
                retry_suffix = (
                    "\n\n【本次局部重试的校验反馈】\n"
                    + render_retry_feedback([str(last_error)])
                    + "\n请重新阅读 PDF，并仅输出当前字段的完整 JSON；"
                    "不要复述或修补上一次答案。"
                )
            try:
                candidate, call_metrics = await invoke_json(
                    client=shared_client,
                    model=mllm["model"],
                    prompt=common_prefix,
                    images=shared_images,
                    prompt_suffix=base_prompt_suffix + retry_suffix,
                    schema=field_schema,
                    schema_name=(
                        f"attribute_field_{field_index:02d}_{field_id}_attempt_{attempt}"
                    ),
                    generation=mllm["generation"],
                    model_request_limiter=model_request_limiter,
                )
                attempt_metrics.append(call_metrics)
                actual_ids = set(candidate.fields)
                if actual_ids != {field_id}:
                    raise StructuredOutputError(
                        f"字段 {field_id} 的响应覆盖异常："
                        f"缺少 {sorted({field_id} - actual_ids)}，"
                        f"额外 {sorted(actual_ids - {field_id})}",
                        finish_reason=None,
                        metrics=call_metrics,
                    )
                candidate_field = finalize_candidate_field(candidate.fields[field_id])
                errors = [
                    *validate_extracted_field(field_id, candidate_field),
                    *validate_attribute_business_rules(field_id, candidate_field),
                ]
                if errors:
                    raise StructuredOutputError(
                        "模型响应未通过 Attribute 业务校验：" + "；".join(errors),
                        finish_reason=None,
                        metrics=call_metrics,
                    )
                finalized = candidate_field
                break
            except StructuredOutputError as error:
                if not attempt_metrics or attempt_metrics[-1] is not error.metrics:
                    attempt_metrics.append(error.metrics)
                last_error = error

        field_metrics = aggregate_attempt_metrics(attempt_metrics)
        if finalized is None:
            assert last_error is not None
            field_records.append(
                {
                    "field_index": field_index,
                    "field_id": field_id,
                    "status": "failed",
                    "attempt_count": len(attempt_metrics),
                    "error_type": type(last_error).__name__,
                    "error": str(last_error),
                    "finish_reason": last_error.finish_reason,
                    "metrics": field_metrics,
                }
            )
            continue

        merged_fields[field_id] = finalized
        field_records.append(
            {
                "field_index": field_index,
                "field_id": field_id,
                "status": "succeeded",
                "attempt_count": len(attempt_metrics),
                "metrics": field_metrics,
            }
        )

    if not merged_fields:
        raise RuntimeError("所有 Attribute 字段提取均失败，无法生成合并结果。")
    expected_field_id_set = set(expected_field_ids)
    missing_field_ids = sorted(expected_field_id_set - set(merged_fields))
    invalid_field_envelopes = [
        {"field_id": field_id, "errors": errors}
        for field_id, field in merged_fields.items()
        if (errors := validate_extracted_field(field_id, field))
    ]
    validation = {
        "is_valid": not missing_field_ids and not invalid_field_envelopes,
        "mode": "active_catalog",
        "missing_field_ids": missing_field_ids,
        "invalid_field_envelopes": invalid_field_envelopes,
    }
    metrics = aggregate_field_metrics(field_records)
    metrics["execution_mode"] = "single_field"
    metrics["max_retries_per_field"] = max_retries_per_field
    metrics["unresolved_field_ids"] = missing_field_ids
    return StageResult(
        payload=[
            _final_attribute_item(field_id, merged_fields[field_id])
            for field_id in expected_field_ids
            if field_id in merged_fields
        ],
        validation=validation,
        metrics=metrics,
    )
