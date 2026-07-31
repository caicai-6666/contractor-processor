#!/usr/bin/env python3
"""两步 MLLM Core 字段提取实验的独立入口。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


try:
    from contract_processor.application.prompts.core_fields import build_compact_field_prompt
    from contract_processor.application.schemas.core_extraction import (
        build_core_extraction_schema,
    )
    from contract_processor.domain.enums import FieldKind
    from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
    from dotenv import load_dotenv
    import fitz
    import httpx
    from jsonschema import Draft202012Validator
    from openai import OpenAI
    from pydantic import BaseModel, ConfigDict, Field
    import yaml
except ImportError as error:  # 依赖检查应在发起模型请求前完成，避免产生不完整结果。
    raise SystemExit(
        "缺少实验依赖。请在已激活的 Conda 环境执行：\n"
        'python -m pip install -e ".[experiments]"\n'
        f"原始错误：{error}"
    ) from error


# Step 1 需要穷举金额与费用原文，避免多页价格表被截断。
# Step 2 每次只处理一个字段，使 reason 与标量字段或对象子字段直接对应，并隔离生成干扰。
STEP_1_MAX_COMPLETION_TOKENS = 6144
STEP_2_FIELD_MAX_COMPLETION_TOKENS = 6144
# 本实验不使用工具调用；请求级屏蔽协议标记，避免模型把结构化 JSON 错误收尾为工具块。
DISALLOWED_TOOL_PROTOCOL_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<tool_response>",
    "</tool_response>",
)
SYSTEM_MESSAGE = "你必须以合同图像为准，严格遵守 JSON Schema。"


class StrictModel(BaseModel):
    """所有实验响应都禁止额外字段，让模型输出可被稳定比较。"""

    model_config = ConfigDict(extra="forbid")


class StructuredOutputError(RuntimeError):
    """保留结构化输出失败的结束原因，供上层决定是否降级重试。"""

    def __init__(self, message: str, *, finish_reason: str | None) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason


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


FieldStatus = Literal[
    "found", "not_found", "ambiguous", "conflicting", "not_applicable"
]
PropertyStatus = Literal[
    "found",
    "not_found",
    "ambiguous",
    "conflicting",
    "not_applicable",
    "out_of_scope",
]


class CoreFieldValue(StrictModel):
    """标量、数组等非对象字段按原文、理由、状态和值生成。"""

    raw_value: str | None = Field(
        default=None, description="用于追溯的原始值；不得用模型总结代替原文"
    )
    reason: str = Field(min_length=1, max_length=160)
    status: FieldStatus
    value: Any | None = Field(default=None, description="按字段定义规范化后的值")


class ObjectPropertyValue(StrictModel):
    """对象直属子字段的独立决策包络。"""

    raw_value: str | None = Field(default=None, description="当前子字段相关的最小必要原文")
    reason: str = Field(min_length=1, max_length=160)
    status: PropertyStatus
    value: Any | None = Field(default=None, description="当前子字段的规范值")


class ObjectFieldCandidate(StrictModel):
    """模型生成的对象字段；不让模型决定对象外层状态。"""

    properties: dict[str, ObjectPropertyValue]


class ObjectFieldValue(StrictModel):
    """最终对象字段；外层状态由直属子字段状态确定性汇总。"""

    status: FieldStatus
    properties: dict[str, ObjectPropertyValue]


class CoreExtractionCandidate(StrictModel):
    """模型的单字段响应；判断理由内联于非对象字段或对象子字段。"""

    document_id: str = Field(description="合同标识")
    fields: dict[str, CoreFieldValue | ObjectFieldCandidate]


class CoreExtraction(StrictModel):
    """应用层汇总结果；对象字段已经补入确定性外层状态。"""

    document_id: str = Field(description="合同标识")
    fields: dict[str, CoreFieldValue | ObjectFieldValue] = Field(
        description="以 field_id 为键的全部 Core 字段结果"
    )


class LiteralYamlString(str):
    """标记需要以 YAML 多行块形式输出的原始文本。"""


class ReadableYamlDumper(yaml.SafeDumper):
    """仅增加多行字符串显示能力，仍沿用 SafeDumper 的安全语义。"""


def represent_literal_yaml_string(
    dumper: yaml.SafeDumper, value: LiteralYamlString
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


ReadableYamlDumper.add_representer(LiteralYamlString, represent_literal_yaml_string)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args(root: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行两步合同 Core 字段提取实验")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=None,
        help="可选：覆盖文件内默认 PDF 路径的绝对路径，或相对于项目根目录的路径。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=root / "experiments/outputs/core_field_extraction",
        help="实验结果目录；每次运行会在其中创建时间戳子目录。",
    )
    parser.add_argument("--max-pages", type=int, default=None, help="覆盖配置中的单次最大页数。")
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=None,
        help="临时覆盖 settings.yaml 中的 MLLM context_window_tokens。",
    )
    parser.add_argument("--print-prompts", action="store_true", help="运行前将完整渲染后的提示词打印到终端。")
    return parser.parse_args()


def read_text(path: Path) -> str:
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


def load_settings(root: Path) -> dict[str, Any]:
    load_dotenv(root / ".env")
    with (root / "configs/settings.yaml").open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def resolve_path(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def render_pdf_as_data_urls(pdf_path: Path, max_pages: int) -> tuple[list[dict[str, Any]], int]:
    """将 PDF 临时渲染为内存 PNG；实验不在 artifacts 中保存合同页面。"""
    document = fitz.open(pdf_path)
    try:
        source_page_count = document.page_count
        page_count = min(source_page_count, max_pages)
        if page_count == 0:
            raise ValueError("PDF 不包含可渲染页面。")

        images: list[dict[str, Any]] = []
        for index in range(page_count):
            # 约 144 DPI 是文档清晰度与视觉 token 消耗之间的起始平衡点。
            pixmap = document.load_page(index).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image_bytes = pixmap.tobytes("png")
            data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
            images.append(
                {"page": index + 1, "data_url": data_url, "image_bytes": len(image_bytes)}
            )
        return images, source_page_count
    finally:
        document.close()


def response_format(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """将指定 JSON Schema 交给 vLLM 的 OpenAI 兼容接口进行约束生成。"""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def validate_field_envelope(_field_id: str, field: CoreFieldValue) -> list[str]:
    """校验非对象字段中 JSON Schema 难以表达的跨属性业务约束。"""
    errors: list[str] = []
    empty_statuses = {"not_found", "ambiguous", "conflicting", "not_applicable"}
    if field.status == "found" and field.value is None:
        errors.append("found 状态必须包含非 null value")
    if field.status in empty_statuses and field.value is not None:
        errors.append(f"{field.status} 状态的 value 必须为 null")
    if field.status in empty_statuses and field.raw_value is not None:
        errors.append(f"{field.status} 状态的 raw_value 必须为 null")
    return errors


def validate_property_envelope(
    property_name: str, field: ObjectPropertyValue
) -> list[str]:
    """校验对象子字段状态，并允许保留被排除或冲突候选的原文。"""

    errors: list[str] = []
    if field.status == "found" and field.value is None:
        errors.append("found 状态必须包含非 null value")
    if field.status in {"not_found", "not_applicable"}:
        if field.value is not None:
            errors.append(f"{field.status} 状态的 value 必须为 null")
        if field.raw_value is not None:
            errors.append(f"{field.status} 状态的 raw_value 必须为 null")
    if field.status in {"ambiguous", "conflicting", "out_of_scope"}:
        if field.value is not None:
            errors.append(f"{field.status} 状态的 value 必须为 null")
        if field.raw_value is None:
            errors.append(f"{field.status} 状态必须保留相关 raw_value")
    return [f"{property_name}: {error}" for error in errors]


def validate_extracted_field(
    field_id: str, field: CoreFieldValue | ObjectFieldCandidate | ObjectFieldValue
) -> list[str]:
    """统一校验标量字段和对象直属子字段包络。"""

    if isinstance(field, CoreFieldValue):
        return validate_field_envelope(field_id, field)
    errors = [
        error
        for property_name, property_value in field.properties.items()
        for error in validate_property_envelope(property_name, property_value)
    ]
    if isinstance(field, ObjectFieldValue):
        expected_status = aggregate_object_status(field.properties)
        if field.status != expected_status:
            errors.append(
                f"对象外层 status 应由子字段汇总为 {expected_status}，实际为 {field.status}"
            )
    return errors


def aggregate_object_status(
    properties: dict[str, ObjectPropertyValue],
) -> FieldStatus:
    """确定性汇总对象状态，局部空值或 out_of_scope 不污染已有 found 结果。"""

    statuses = {property_value.status for property_value in properties.values()}
    if "found" in statuses:
        return "found"
    if "conflicting" in statuses:
        return "conflicting"
    if "ambiguous" in statuses:
        return "ambiguous"
    if statuses == {"not_applicable"}:
        return "not_applicable"
    return "not_found"


def finalize_candidate_field(
    field: CoreFieldValue | ObjectFieldCandidate,
) -> CoreFieldValue | ObjectFieldValue:
    """把模型对象候选转换为带确定性外层状态的最终字段。"""

    if isinstance(field, CoreFieldValue):
        return field
    return ObjectFieldValue(
        status=aggregate_object_status(field.properties),
        properties=field.properties,
    )


def build_page_visibility_context(images: list[dict[str, Any]], source_page_count: int) -> str:
    """明确模型可见范围，防止按常见合同结构臆测未输入页面。"""
    rendered_pages = ", ".join(str(image["page"]) for image in images)
    return (
        "【页面可见范围（程序提供，优先级最高）】\n"
        f"- 原始 PDF 的物理页数：{source_page_count}。\n"
        f"- 本次仅向你提供物理第 {rendered_pages} 页的图像。\n"
        "- 只能描述已提供图像中实际可见的内容；不得根据合同常见结构、标题或常识推断未输入页面。\n"
        "- 对合同内印刷的页码或“共 X 页”等标识，只能依据图像中可见的页眉、页脚、页码或同等明确标识；看不到则输出未发现，不得猜测。\n"
        "- 结构化证据中的 page 使用本次提供图像对应的物理 PDF 页码，不代表模型推断的合同内部印刷页码。"
    )


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


def invoke_json(
    *,
    client: OpenAI,
    model: str,
    prompt: str,
    images: list[dict[str, Any]],
    schema_model: type[BaseModel],
    schema_name: str,
    json_schema: dict[str, Any] | None = None,
    generation: dict[str, Any],
    max_completion_tokens: int,
    prompt_suffix: str | None = None,
    raw_response_path: Path | None = None,
    failure_metrics_path: Path | None = None,
) -> tuple[BaseModel, dict[str, Any], dict[str, Any]]:
    effective_schema = json_schema or schema_model.model_json_schema()
    started_at = time.perf_counter()
    completion = client.chat.completions.create(
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
    if raw_response_path:
        write_raw_response(raw_response_path, raw)
    if failure_metrics_path:
        write_json(failure_metrics_path, metrics)

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
            "模型响应未通过 JSON Schema 校验；原始响应和 usage 已保存。"
            f"finish_reason={finish_reason!r}，请优先检查是否为长度截断。",
            finish_reason=finish_reason,
        ) from error
    return parsed, metrics, raw


def verify_vllm_connection(client: OpenAI, base_url: str) -> None:
    """在渲染和推理前验证本地服务可达，避免将连接问题误判成提取失败。"""
    try:
        client.models.list()
    except Exception as error:
        raise SystemExit(
            f"无法连接本地 vLLM：{base_url}。请确认服务已启动、端口配置正确，"
            "且模型服务可从当前终端访问。"
        ) from error


def write_json(path: Path, content: Any) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_raw_response(json_path: Path, raw_response: dict[str, Any]) -> None:
    """同时保存机器可读 JSON 与方便人工查看的 YAML 原始响应。"""
    write_json(json_path, raw_response)
    yaml_response = json.loads(json.dumps(raw_response, ensure_ascii=False))
    choices = yaml_response.get("choices")
    message = choices[0].get("message", {}) if isinstance(choices, list) and choices else {}
    content = message.get("content")
    if isinstance(content, str):
        try:
            # 完整 JSON 回复在 YAML 中展开；截断回复仍保留字符串，以便直接定位损坏位置。
            message["content"] = json.loads(content)
        except json.JSONDecodeError:
            message["content"] = LiteralYamlString(content)
    json_path.with_suffix(".yaml").write_text(
        yaml.dump(
            yaml_response,
            Dumper=ReadableYamlDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        ),
        encoding="utf-8",
    )


def print_metrics(step_name: str, metrics: dict[str, Any], max_model_len: int) -> None:
    usage = metrics["usage"]
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    print(f"\n[{step_name}]")
    print(f"  prompt_tokens: {prompt_tokens if prompt_tokens is not None else '服务未返回'}")
    print(f"  completion_tokens: {completion_tokens if completion_tokens is not None else '服务未返回'}")
    print(f"  total_tokens: {total_tokens if total_tokens is not None else '服务未返回'}")
    print(f"  configured_max_model_len: {max_model_len}")
    if total_tokens is not None:
        print(f"  remaining_context_tokens: {max_model_len - total_tokens}")
    print(f"  prompt_text_characters: {metrics['prompt_text_characters']}")
    print(f"  image_count / image_bytes: {metrics['image_count']} / {metrics['image_bytes']}")
    print(f"  elapsed_seconds: {metrics['elapsed_seconds']}")


def aggregate_field_metrics(field_records: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总逐字段调用指标，同时保留每个字段结果，避免聚合值掩盖局部失败。"""

    successful_count = sum(record["status"] == "succeeded" for record in field_records)
    failed_count = sum(record["status"] == "failed" for record in field_records)
    aggregate_usage = {
        key: sum(
            int(record.get("metrics", {}).get("usage", {}).get(key) or 0)
            for record in field_records
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return {
        "field_count": len(field_records),
        "successful_field_count": successful_count,
        "failed_field_count": failed_count,
        "aggregate_elapsed_seconds": round(
            sum(
                float(record.get("metrics", {}).get("elapsed_seconds") or 0)
                for record in field_records
            ),
            3,
        ),
        "aggregate_usage": aggregate_usage,
        "fields": field_records,
    }


def main(default_pdf_path: Path | None = None) -> None:
    """运行实验；IDE 直接启动时由 __main__ 中的路径变量传入默认 PDF。"""
    root = project_root()
    args = parse_args(root)
    settings = load_settings(root)
    mllm = settings["models"]["mllm"]
    requested_pdf_path = args.pdf or default_pdf_path
    if requested_pdf_path is None:
        raise SystemExit("请在 __main__ 的 DEFAULT_PDF_PATH 中设置路径，或通过 --pdf 传入 PDF。")
    pdf_path = resolve_path(requested_pdf_path, root)
    if not pdf_path.is_file():
        raise SystemExit(f"找不到待分析 PDF：{pdf_path}")

    max_pages = args.max_pages or mllm["vision"]["max_pages_per_request"]
    if max_pages < 1:
        raise SystemExit("--max-pages 必须大于 0。")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = resolve_path(args.output_dir, root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    images, source_page_count = render_pdf_as_data_urls(pdf_path, max_pages)
    prompt_dir = root / "experiments/core_field_extraction/prompts"
    shared_prompt_dir = root / "experiments/prompts"
    common_prefix = read_text(shared_prompt_dir / "00_contract_pdf_common_prefix.txt")
    page_visibility_context = build_page_visibility_context(images, source_page_count)
    understanding_prompt = read_text(prompt_dir / "01_understand_contract.txt").replace(
        "{{PAGE_VISIBILITY_CONTEXT}}", page_visibility_context
    )
    core_yaml_path = root / settings["paths"]["core_fields"]
    core_yaml = read_text(core_yaml_path)
    # 提前解析 YAML，避免模型已调用后才发现字段定义文件无效。
    core_payload = yaml.safe_load(core_yaml)
    core_fields = core_payload["fields"]
    field_catalog = YamlFieldCatalog(
        core_path=core_yaml_path,
        attribute_path=root / settings["paths"]["attribute_fields"],
    )
    core_field_definitions = list(field_catalog.load(FieldKind.CORE))
    definitions_by_id = {field.field_id: field for field in core_field_definitions}
    expected_field_ids = {field["field_id"] for field in core_fields}
    if set(definitions_by_id) != expected_field_ids:
        raise ValueError("字段目录对象与 Core YAML 的 field_id 不一致。")
    write_json(
        run_dir / "run_manifest.json",
        {
            "run_id": run_id,
            "pdf_path": str(pdf_path),
            "pdf_name": pdf_path.name,
            "model": mllm["model"],
            "base_url": mllm["base_url"],
            "context_window_tokens": args.max_model_len or mllm["context_window_tokens"],
            "source_pdf_page_count": source_page_count,
            "rendered_pages": [image["page"] for image in images],
            "core_field_ids": sorted(expected_field_ids),
            "core_schema_version": core_payload.get("schema_version"),
            "stable_prefix_layout": "shared_text_then_images_then_task",
            "step2_execution_mode": "single_field",
            "step2_object_envelope": "direct_property_decisions",
            "step2_reason_position": "before_status",
            "step1_max_completion_tokens": min(
                mllm["generation"]["max_completion_tokens"],
                STEP_1_MAX_COMPLETION_TOKENS,
            ),
            "step2_understanding_context_format": "bullet",
            "step2_field_max_completion_tokens": min(
                mllm["generation"]["max_completion_tokens"],
                STEP_2_FIELD_MAX_COMPLETION_TOKENS,
            ),
            "core_extraction_fields": [field["field_id"] for field in core_fields],
        },
    )
    (run_dir / "00_common_prefix_prompt.txt").write_text(
        common_prefix, encoding="utf-8"
    )
    (run_dir / "01_understand_contract_prompt.txt").write_text(
        understanding_prompt, encoding="utf-8"
    )

    api_key = os.getenv(mllm["api_key_env"]) or "EMPTY"
    # vLLM 部署在本机时不应继承终端的 HTTP/SOCKS 代理，避免本地请求被代理依赖阻断。
    http_client = httpx.Client(timeout=mllm["timeout_seconds"], trust_env=False)
    client = OpenAI(base_url=mllm["base_url"], api_key=api_key, http_client=http_client)
    verify_vllm_connection(client, mllm["base_url"])
    print(f"已连接本地 vLLM：{mllm['base_url']}（模型：{mllm['model']}）")
    generation = mllm["generation"]
    max_model_len = args.max_model_len or mllm["context_window_tokens"]
    if max_model_len < 1:
        raise SystemExit("上下文长度必须大于 0。")

    if args.print_prompts:
        print("\n===== 公共前缀 =====\n" + common_prefix)
        print("\n===== Step 1 任务后缀 =====\n" + understanding_prompt)

    understanding, step1_metrics, step1_raw = invoke_json(
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
        raw_response_path=run_dir / "01_raw_response.json",
        failure_metrics_path=run_dir / "01_failure_metrics.json",
    )
    understanding_json = understanding.model_dump_json(indent=2)
    (run_dir / "01_contract_understanding.json").write_text(
        understanding_json, encoding="utf-8"
    )
    understanding_bullets = render_contract_understanding_bullets(understanding)
    (run_dir / "01_contract_understanding_bullets.txt").write_text(
        understanding_bullets, encoding="utf-8"
    )
    write_raw_response(run_dir / "01_raw_response.json", step1_raw)
    print_metrics("Step 1: 合同理解", step1_metrics, max_model_len)

    extraction_template = read_text(prompt_dir / "02_extract_core.txt")
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
        ).replace("{{PAGE_VISIBILITY_CONTEXT}}", page_visibility_context)
    )
    (run_dir / "02_extract_core_common_prompt.txt").write_text(
        extraction_common_prompt, encoding="utf-8"
    )
    field_root = run_dir / "02_fields"
    field_root.mkdir()
    merged_fields: dict[str, CoreFieldValue | ObjectFieldValue] = {}
    document_id: str | None = None
    document_id_mismatches: list[dict[str, Any]] = []
    field_records: list[dict[str, Any]] = []

    # 每次调用只生成一个 Core 字段，使标量 reason 或对象子字段 reasons 与判断对象直接绑定。
    # 字段失败只隔离当前字段并继续后续字段，不再创建语义相同的拆分重试。
    for field_index, field in enumerate(core_fields, start=1):
        field_id = field["field_id"]
        attempt_id = f"{field_index:02d}"
        field_dir = field_root / f"{field_index:02d}_{field_id}"
        field_dir.mkdir()
        field_schema = build_core_extraction_schema([field])
        field_prompt_suffix = (
            build_compact_field_prompt([definitions_by_id[field_id]])
            + extraction_suffix_template
        )
        write_json(field_dir / "schema.json", field_schema)
        (field_dir / "prompt_suffix.txt").write_text(
            field_prompt_suffix, encoding="utf-8"
        )
        if args.print_prompts:
            print(
                f"\n===== Step 2 field {attempt_id}: {field_id} =====\n"
                f"{common_prefix}\n<合同页面图像>\n"
                f"{extraction_common_prompt}{field_prompt_suffix}"
            )

        metrics_path = field_dir / "metrics.json"
        try:
            field_candidate, field_metrics, field_raw_response = invoke_json(
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
                raw_response_path=field_dir / "raw_response.json",
                failure_metrics_path=metrics_path,
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
                )
        except StructuredOutputError as error:
            # 仅隔离当前字段的模型输出失败；连接、鉴权等基础设施错误仍立即终止。
            field_metrics = (
                json.loads(metrics_path.read_text(encoding="utf-8"))
                if metrics_path.is_file()
                else {}
            )
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
            write_json(field_dir / "failure.json", failure)
            field_records.append(failure)
            print(f"\n[Step 2 字段 {attempt_id}: {field_id}] 失败：{error}")
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

        if document_id is None:
            document_id = field_candidate.document_id
        elif field_candidate.document_id != document_id:
            document_id_mismatches.append(
                {
                    "field_index": field_index,
                    "attempt_id": attempt_id,
                    "field_id": field_id,
                    "expected": document_id,
                    "actual": field_candidate.document_id,
                }
            )
        finalized_field = finalize_candidate_field(field_candidate.fields[field_id])
        finalized_extraction = CoreExtraction(
            document_id=field_candidate.document_id,
            fields={field_id: finalized_field},
        )
        merged_fields[field_id] = finalized_field
        write_json(
            field_dir / "extraction.json",
            finalized_extraction.model_dump(mode="json"),
        )
        record = {
            "field_index": field_index,
            "attempt_id": attempt_id,
            "field_id": field_id,
            "status": "succeeded",
            "document_id": field_candidate.document_id,
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
            "metrics": field_metrics,
        }
        field_records.append(record)
        print_metrics(
            f"Step 2 字段 {attempt_id}: {field_id}",
            field_metrics,
            max_model_len,
        )

    step2_metrics = aggregate_field_metrics(field_records)
    step2_metrics["execution_mode"] = "single_field"
    step2_metrics["document_id_mismatches"] = document_id_mismatches
    step2_metrics["unresolved_field_ids"] = sorted(expected_field_ids - set(merged_fields))
    write_json(run_dir / "02_field_manifest.json", step2_metrics)
    if document_id is None:
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
    write_json(run_dir / "02_core_extraction.json", extraction.model_dump(mode="json"))

    validation = {
        "missing_field_ids": missing_ids,
        "unexpected_field_ids": unexpected_ids,
        "invalid_field_envelopes": invalid_envelopes,
    }
    write_json(run_dir / "field_coverage_validation.json", validation)
    # 当前实验只保留理解与抽取两步；最终结果即 Step 2 的确定性合并结果。
    write_json(run_dir / "final_core_extraction.json", extraction.model_dump(mode="json"))
    metrics = {
        "max_model_len": max_model_len,
        "step_1": step1_metrics,
        "step_2": step2_metrics,
    }
    write_json(run_dir / "metrics.json", metrics)
    print("\n最终提取结果：")
    print(json.dumps(extraction.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if missing_ids or unexpected_ids or invalid_envelopes:
        print("\n警告：字段覆盖不完整，详见 field_coverage_validation.json。")
    print(f"\n完整实验产物已写入：{run_dir}")


if __name__ == "__main__":
    # 在 IDE 中直接点击运行前，只需修改这一项；可填绝对路径或项目根目录相对路径。
    DEFAULT_PDF_PATH = Path("data/input/ET-3030加热台合同2025-04-03_已签章.pdf")
    try:
        main(default_pdf_path=DEFAULT_PDF_PATH)
    except SystemExit:
        raise
    except Exception as error:
        print(f"实验失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
