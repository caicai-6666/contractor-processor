#!/usr/bin/env python3
"""分阶段 MLLM Clause 条款提取实验的独立入口。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

try:
    from dotenv import load_dotenv
    import fitz
    import httpx
    from jsonschema import Draft202012Validator
    from openai import OpenAI
    from pydantic import BaseModel, ConfigDict, Field, PositiveInt
    import yaml
except ImportError as error:  # 依赖检查应在模型调用前完成，避免留下不完整实验目录。
    raise SystemExit(
        "缺少实验依赖。请在已激活的 Conda 环境执行：\n"
        'python -m pip install -e ".[experiments]"\n'
        f"原始错误：{error}"
    ) from error


STEP_1_MAX_COMPLETION_TOKENS = 6144
STEP_1B_MAX_COMPLETION_TOKENS = 4096
STEP_2_CANDIDATE_MAX_COMPLETION_TOKENS = 512
STEP_3_UNIT_MAX_COMPLETION_TOKENS = 8192
DISALLOWED_TOOL_PROTOCOL_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<tool_response>",
    "</tool_response>",
)
SYSTEM_MESSAGE = "你必须以合同图像为准，严格遵守 JSON Schema。"


class StrictModel(BaseModel):
    """Clause 实验的所有模型响应均禁止额外字段。"""

    model_config = ConfigDict(extra="forbid")


class StructuredOutputError(RuntimeError):
    """保留结构化输出的结束原因，供单元级失败隔离使用。"""

    def __init__(self, message: str, *, finish_reason: str | None) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason


class ClauseStructureCandidate(StrictModel):
    """Step 1 按视觉顺序发现的结构候选，尚未判断其是否属于 Clause。"""

    parent_clause_number: str | None = Field(default=None, min_length=1, max_length=40)
    parent_heading: str | None = Field(default=None, min_length=1, max_length=160)
    item_marker: str | None = Field(default=None, min_length=1, max_length=40)
    item_heading: str | None = Field(default=None, min_length=1, max_length=160)
    opening_anchor: str = Field(min_length=1, max_length=200)
    page_refs: list[PositiveInt] = Field(min_length=1)


class ClauseStructureMap(StrictModel):
    """Step 1 穷举得到的结构候选地图。"""

    candidates: list[ClauseStructureCandidate] = Field(default_factory=list)


class ClauseBoundaryGroup(StrictModel):
    """Step 1B 只用索引表达合并边界和标题采用策略。"""

    source_candidate_indices: list[PositiveInt] = Field(min_length=1)
    heading_strategy: Literal["own", "inherit_parent"]
    heading_candidate_index: PositiveInt


class ClauseBoundaryPlan(StrictModel):
    """全部 Step 1 原子必须恰好进入一个非重叠分组或忽略列表。"""

    groups: list[ClauseBoundaryGroup] = Field(default_factory=list)
    ignored_candidate_indices: list[PositiveInt] = Field(default_factory=list)


class ClauseReviewProjection(StrictModel):
    """程序从 Step 1 派生的 Step 2 只读候选，不让模型重写合同地图。"""

    candidate_index: PositiveInt
    fused_heading: str | None = Field(default=None, min_length=1, max_length=325)
    location: str = Field(min_length=1, max_length=400)


class ClauseRetentionDecision(StrictModel):
    """Step 2 对单个投影候选只判断去留。"""

    reason: str = Field(min_length=1, max_length=200)
    decision: Literal["include", "exclude"]


class ConsolidatedClauseCandidate(ClauseStructureCandidate):
    """程序按 Step 1B 索引分组生成的非重叠 Clause 候选。"""

    source_candidate_indices: list[PositiveInt] = Field(min_length=1)
    end_before_anchor: str | None = Field(default=None, min_length=1, max_length=200)


class ResolvedClauseUnit(StrictModel):
    """程序解析标题来源和引用编号后交给 Step 3 的确定性提取单元。"""

    source_candidate_indices: list[PositiveInt] = Field(min_length=1)
    clause_number: str | None = Field(default=None, min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    heading_source: Literal["own", "inherited"]
    opening_anchor: str = Field(min_length=1, max_length=200)
    end_before_anchor: str | None = Field(default=None, min_length=1, max_length=200)
    page_refs: list[PositiveInt] = Field(min_length=1)


class ClauseItem(StrictModel):
    """最终 Clause 最小结构，与 clause.yaml 保持一致。"""

    clause_number: str | None = Field(default=None, min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    source_text: str = Field(min_length=1)
    page_refs: list[PositiveInt] = Field(min_length=1)


class ClauseUnitExtraction(StrictModel):
    """Step 3 每次只抽取一个已经通过复核的 Clause 单元。"""

    clauses: list[ClauseItem] = Field(min_length=1, max_length=1)


class ClauseExtraction(StrictModel):
    """程序按复核顺序确定性合并后的合同级 Clause 结果。"""

    clauses: list[ClauseItem]


class LiteralYamlString(str):
    """标记需要以 YAML 多行块形式展示的模型文本。"""


class ReadableYamlDumper(yaml.SafeDumper):
    """仅调整多行字符串的展示形式。"""


def represent_literal_yaml_string(
    dumper: yaml.SafeDumper, value: LiteralYamlString
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


ReadableYamlDumper.add_representer(LiteralYamlString, represent_literal_yaml_string)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args(root: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行分阶段合同 Clause 条款提取实验")
    parser.add_argument("--pdf", type=Path, default=None, help="覆盖默认 PDF 路径。")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=root / "experiments/outputs/clause_extraction",
        help="实验结果根目录；每次运行会创建 UTC 时间戳子目录。",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None, help="整份 PDF 允许的最大页数安全上限。"
    )
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=None,
        help="临时覆盖 settings.yaml 中的 MLLM context_window_tokens。",
    )
    parser.add_argument("--print-prompts", action="store_true", help="打印完整提示词。")
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_settings(root: Path) -> dict[str, Any]:
    load_dotenv(root / ".env")
    with (root / "configs/settings.yaml").open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def resolve_path(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def render_entire_pdf_as_data_urls(
    pdf_path: Path, max_pages: int
) -> tuple[list[dict[str, Any]], int]:
    """将整份 PDF 渲染到内存；超过安全上限时拒绝静默截断。"""

    document = fitz.open(pdf_path)
    try:
        source_page_count = document.page_count
        if source_page_count == 0:
            raise ValueError("PDF 不包含可渲染页面。")
        if source_page_count > max_pages:
            raise ValueError(
                f"PDF 共 {source_page_count} 页，超过当前整份输入安全上限 {max_pages} 页。"
                "请同时确认 vLLM --limit-mm-per-prompt 和上下文容量后，提高 --max-pages；"
                "Clause 实验不会截断合同。"
            )
        images: list[dict[str, Any]] = []
        for index in range(source_page_count):
            pixmap = document.load_page(index).get_pixmap(
                matrix=fitz.Matrix(2, 2), alpha=False
            )
            image_bytes = pixmap.tobytes("png")
            images.append(
                {
                    "page": index + 1,
                    "data_url": "data:image/png;base64,"
                    + base64.b64encode(image_bytes).decode("ascii"),
                    "image_bytes": len(image_bytes),
                }
            )
        return images, source_page_count
    finally:
        document.close()


def build_page_visibility_context(source_page_count: int) -> str:
    rendered_pages = ", ".join(str(page) for page in range(1, source_page_count + 1))
    return (
        "【页面可见范围（程序提供，优先级最高）】\n"
        f"- 原始 PDF 共 {source_page_count} 个物理页，本次提供全部页面：{rendered_pages}。\n"
        "- page_refs 只能使用上述物理页码，不得根据合同内部印刷页码推断。\n"
        "- 同一内容跨页时，应保留正文实际出现的全部物理页。"
    )


def messages_for(
    common_prefix: str, images: list[dict[str, Any]], task_suffix: str
) -> list[dict[str, Any]]:
    """固定说明和整份 PDF 位于可变任务之前，以便各阶段复用前缀。"""

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


def write_json(path: Path, content: Any) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_raw_response(json_path: Path, raw_response: dict[str, Any]) -> None:
    write_json(json_path, raw_response)
    yaml_response = json.loads(json.dumps(raw_response, ensure_ascii=False))
    choices = yaml_response.get("choices")
    message = choices[0].get("message", {}) if isinstance(choices, list) and choices else {}
    content = message.get("content")
    if isinstance(content, str):
        try:
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


def invoke_json(
    *,
    client: OpenAI,
    model: str,
    common_prefix: str,
    images: list[dict[str, Any]],
    task_suffix: str,
    schema_model: type[BaseModel],
    schema_name: str,
    generation: dict[str, Any],
    max_completion_tokens: int,
    raw_response_path: Path,
    metrics_path: Path,
) -> tuple[BaseModel, dict[str, Any], dict[str, Any]]:
    schema = schema_model.model_json_schema()
    started_at = time.perf_counter()
    completion = client.chat.completions.create(
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
    raw = completion.model_dump(mode="json")
    write_raw_response(raw_response_path, raw)
    write_json(metrics_path, metrics)
    content = completion.choices[0].message.content
    if not content:
        raise StructuredOutputError(
            "模型未返回 JSON 内容。请检查 vLLM structured outputs 配置与服务日志。",
            finish_reason=completion.choices[0].finish_reason,
        )
    try:
        decoded = json.loads(content)
        Draft202012Validator(schema).validate(decoded)
        parsed = schema_model.model_validate(decoded)
    except Exception as error:
        finish_reason = completion.choices[0].finish_reason
        raise StructuredOutputError(
            "模型响应未通过 JSON Schema 校验；原始响应和指标已保存。"
            f"finish_reason={finish_reason!r}，请优先检查长度截断。",
            finish_reason=finish_reason,
        ) from error
    return parsed, metrics, raw


def validate_page_refs(
    page_refs: list[int], source_page_count: int, *, location: str
) -> list[str]:
    errors: list[str] = []
    if page_refs != sorted(page_refs):
        errors.append(f"{location}: page_refs 必须按升序输出")
    if len(page_refs) != len(set(page_refs)):
        errors.append(f"{location}: page_refs 不得重复")
    invalid_pages = [page for page in page_refs if not 1 <= page <= source_page_count]
    if invalid_pages:
        errors.append(f"{location}: 包含不可见物理页码 {invalid_pages}")
    return errors


def validate_structure_map(
    structure_map: ClauseStructureMap, source_page_count: int
) -> list[str]:
    return [
        error
        for index, candidate in enumerate(structure_map.candidates, start=1)
        for error in validate_page_refs(
            candidate.page_refs, source_page_count, location=f"candidates[{index}]"
        )
    ]


def validate_boundary_plan(
    plan: ClauseBoundaryPlan, structure_map: ClauseStructureMap
) -> list[str]:
    """确保分组全局有序、不重叠，并完整核销每个 Step 1 原子。"""

    errors: list[str] = []
    candidate_count = len(structure_map.candidates)
    grouped_indices: list[int] = []
    previous_last = 0
    for group_index, group in enumerate(plan.groups, start=1):
        indices = [int(index) for index in group.source_candidate_indices]
        invalid_indices = [
            index for index in indices if not 1 <= index <= candidate_count
        ]
        if invalid_indices:
            errors.append(
                f"groups[{group_index}]: source_candidate_indices 超出范围 "
                f"{invalid_indices}"
            )
        if indices != list(range(indices[0], indices[-1] + 1)):
            errors.append(f"groups[{group_index}]: source_candidate_indices 必须连续")
        if indices[0] <= previous_last:
            errors.append(f"groups[{group_index}]: 分组必须按原文顺序且不得重叠")
        previous_last = indices[-1]
        grouped_indices.extend(indices)
        heading_index = int(group.heading_candidate_index)
        if heading_index != indices[0]:
            errors.append(
                f"groups[{group_index}]: heading_candidate_index 必须是分组首项"
            )
        if not 1 <= heading_index <= candidate_count:
            errors.append(f"groups[{group_index}]: heading_candidate_index 超出范围")
            continue
        heading_candidate = structure_map.candidates[heading_index - 1]
        if group.heading_strategy == "own" and not (
            heading_candidate.item_heading or heading_candidate.parent_heading
        ):
            errors.append(f"groups[{group_index}]: own 策略没有可采用的原文明示标题")
        if group.heading_strategy == "inherit_parent" and (
            not heading_candidate.parent_heading or not heading_candidate.item_marker
        ):
            errors.append(
                f"groups[{group_index}]: inherit_parent 策略必须同时具有父标题和子项标记"
            )

    ignored = [int(index) for index in plan.ignored_candidate_indices]
    if ignored != sorted(set(ignored)):
        errors.append("ignored_candidate_indices 必须升序且不得重复")
    accounted = grouped_indices + ignored
    expected = list(range(1, candidate_count + 1))
    if sorted(accounted) != expected or len(accounted) != len(set(accounted)):
        errors.append(
            "groups 与 ignored_candidate_indices 必须恰好核销每个 Step 1 候选一次"
        )
    return errors


NUMBERED_MARKER_PATTERN = re.compile(
    r"^(?:[（(][0-9一二三四五六七八九十百]+[）)]|"
    r"[0-9]+(?:\.[0-9]+)+[.．]?|"
    r"[0-9一二三四五六七八九十百]+[、.．)])$"
)
LEADING_MARKER_PATTERN = re.compile(
    r"^\s*(?P<marker>◆|●|•|▪|■|▶|►|[-–—]|"
    r"[（(][0-9一二三四五六七八九十百]+[）)]|"
    r"[0-9]+(?:\.[0-9]+)+[.．]?|"
    r"[0-9一二三四五六七八九十百]+[、.．)])\s*"
)
LEADING_LABEL_PATTERN = re.compile(r"^(?P<label>[^：:，,。；;\n]{1,40})\s*[：:]")


def compact_number_token(value: str) -> str:
    """仅用于比较编号层级，保留括号并去除空白和末尾分隔符。"""

    return re.sub(r"[、.．]+$", "", re.sub(r"\s+", "", value))


def marker_repeats_parent_number(parent: str | None, marker: str | None) -> bool:
    """识别“parent=七、marker=七、”这类同一个顶层编号的重复解析。"""

    if not parent or not marker:
        return False
    # 带括号的（1）表示真实子层级，即使数字恰好与父编号相同也不能去重。
    if marker.strip().startswith(("（", "(")):
        return False
    return compact_number_token(parent) == compact_number_token(marker)


def normalize_candidate_structure(
    candidate: ClauseStructureCandidate,
) -> ClauseStructureCandidate:
    """只解析锚点中的显式标点结构，不依据正文语义创造标题。"""

    updates: dict[str, str] = {}
    marker_match = LEADING_MARKER_PATTERN.match(candidate.opening_anchor)
    marker = candidate.item_marker
    remainder = candidate.opening_anchor.lstrip()
    if marker_match:
        detected_marker = marker_match.group("marker")
        detected_is_current_item = False
        if candidate.item_marker is not None:
            # 锚点可能以父编号开头、真正子标记位于后方；此时不能用父编号解析子标题。
            detected_is_current_item = (
                re.sub(r"\s+", "", candidate.item_marker)
                == re.sub(r"\s+", "", detected_marker)
            )
        elif not marker_repeats_parent_number(
            candidate.parent_clause_number, detected_marker
        ):
            marker = detected_marker
            updates["item_marker"] = marker
            detected_is_current_item = True
        if detected_is_current_item:
            remainder = candidate.opening_anchor[marker_match.end() :].lstrip()
    else:
        detected_is_current_item = False
    if marker and detected_is_current_item and candidate.item_heading is None:
        label_match = LEADING_LABEL_PATTERN.match(remainder)
        if label_match:
            updates["item_heading"] = label_match.group("label").strip()
    return candidate.model_copy(update=updates) if updates else candidate


def normalize_structure_map(
    structure_map: ClauseStructureMap,
) -> tuple[ClauseStructureMap, list[dict[str, Any]]]:
    original_candidates = structure_map.candidates
    normalized_candidates = [
        normalize_candidate_structure(candidate) for candidate in original_candidates
    ]

    # 模型偶尔把父标题单独列为候选。只有“下一项明确带分点标记”或“当前文本明确以冒号
    # 结束并紧邻正文”时才传播，避免仅凭短文本语义猜测标题。
    for index, candidate in enumerate(normalized_candidates[:-1]):
        next_candidate = normalized_candidates[index + 1]
        candidate_has_structure = bool(
            candidate.parent_heading or candidate.item_heading or candidate.item_marker
        )
        if candidate_has_structure:
            continue
        anchor = candidate.opening_anchor.strip()
        title = anchor.rstrip("：:").strip()
        if not title or len(title) > 160:
            continue
        if next_candidate.item_marker:
            normalized_candidates[index] = candidate.model_copy(
                update={"parent_heading": title}
            )
            child_index = index + 1
            while child_index < len(normalized_candidates):
                child = normalized_candidates[child_index]
                if not child.item_marker:
                    break
                if child.parent_heading is None:
                    normalized_candidates[child_index] = child.model_copy(
                        update={"parent_heading": title}
                    )
                child_index += 1
        elif anchor.endswith(("：", ":")) and not (
            next_candidate.parent_heading or next_candidate.item_heading
        ):
            normalized_candidates[index] = candidate.model_copy(
                update={"parent_heading": title}
            )
            normalized_candidates[index + 1] = next_candidate.model_copy(
                update={"parent_heading": title}
            )

    changes: list[dict[str, Any]] = []
    for index, (before, after) in enumerate(
        zip(original_candidates, normalized_candidates, strict=True), start=1
    ):
        if after != before:
            changes.append(
                {
                    "candidate_index": index,
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                }
            )
    return ClauseStructureMap(candidates=normalized_candidates), changes


def fuse_candidate_headings(candidate: ClauseStructureCandidate) -> str | None:
    """父标题与自身标题按非空部分融合；相同标题只保留一次。"""

    headings = [
        heading.strip()
        for heading in (candidate.parent_heading, candidate.item_heading)
        if heading and heading.strip()
    ]
    unique_headings = list(dict.fromkeys(headings))
    return " / ".join(unique_headings) if unique_headings else None


def render_indexed_structure_map(structure_map: ClauseStructureMap) -> str:
    """为 Step 1B 加入稳定的一基索引，模型只能引用索引而不能重写原子。"""

    return render_yaml(
        {
            "candidates": [
                {"candidate_index": index, **candidate.model_dump(mode="json")}
                for index, candidate in enumerate(structure_map.candidates, start=1)
            ]
        }
    )


def build_consolidated_candidates(
    plan: ClauseBoundaryPlan, structure_map: ClauseStructureMap
) -> list[ConsolidatedClauseCandidate]:
    """按索引计划确定性合并边界，不让模型复制或改写 Step 1 结构事实。"""

    candidates = structure_map.candidates
    consolidated: list[ConsolidatedClauseCandidate] = []
    for group in plan.groups:
        indices = [int(index) for index in group.source_candidate_indices]
        heading_candidate = candidates[int(group.heading_candidate_index) - 1]
        last_index = indices[-1]
        # 下一原子即当前组的硬结束边界；即使下一原子最终被忽略，也不能吞入本条款。
        end_before_anchor = (
            candidates[last_index].opening_anchor
            if last_index < len(candidates)
            else None
        )
        pages = sorted(
            {
                page
                for source_index in indices
                for page in candidates[source_index - 1].page_refs
            }
        )
        heading_updates: dict[str, str | None] = (
            {"item_heading": None}
            if group.heading_strategy == "inherit_parent"
            else {}
        )
        candidate_data = heading_candidate.model_copy(
            update=heading_updates
        ).model_dump(mode="python")
        candidate_data["page_refs"] = pages
        consolidated.append(
            ConsolidatedClauseCandidate(
                **candidate_data,
                source_candidate_indices=indices,
                end_before_anchor=end_before_anchor,
            )
        )
    return consolidated


def render_candidate_location(
    candidate_index: int, candidate: ConsolidatedClauseCandidate
) -> str:
    """位置提供边界组、页码和首尾锚点，不传递可重写的地图对象。"""

    pages = "、".join(str(page) for page in candidate.page_refs)
    sources = "、".join(str(index) for index in candidate.source_candidate_indices)
    marker = f"；原文标记：{candidate.item_marker}" if candidate.item_marker else ""
    boundary = (
        f"；在此锚点前结束：{candidate.end_before_anchor}"
        if candidate.end_before_anchor
        else "；结束于文件末尾"
    )
    return (
        f"条款组序号 {candidate_index}；Step 1 原子 {sources}；物理页 {pages}{marker}；"
        f"开始锚点：{candidate.opening_anchor}{boundary}"
    )


def build_review_projections(
    candidates: list[ConsolidatedClauseCandidate],
) -> list[ClauseReviewProjection]:
    """把非重叠条款组压缩为 Step 2 所需的最小只读视图。"""

    return [
        ClauseReviewProjection(
            candidate_index=index,
            fused_heading=fuse_candidate_headings(candidate),
            location=render_candidate_location(index, candidate),
        )
        for index, candidate in enumerate(candidates, start=1)
    ]


def validate_retention_decision(
    projection: ClauseReviewProjection, decision: ClauseRetentionDecision
) -> list[str]:
    """没有父标题和自身标题的候选不能取得 Clause 资格。"""

    if decision.decision == "include" and projection.fused_heading is None:
        return [
            f"candidate[{projection.candidate_index}]: fused_heading 为 null 时必须 exclude"
        ]
    return []


def select_reviewed_candidates(
    candidates: list[ConsolidatedClauseCandidate],
    decisions: list[ClauseRetentionDecision | None],
) -> list[ConsolidatedClauseCandidate]:
    """模型只决定去留；程序始终保留 Step 1B 确定的完整边界。"""

    return [
        candidate
        for candidate, decision in zip(candidates, decisions, strict=True)
        if decision is not None and decision.decision == "include"
    ]


def collect_resolution_errors(
    units: list[ClauseStructureCandidate],
) -> list[str]:
    """一次报告所有不可解析标题，避免只暴露第一个模型错误。"""

    return [
        f"extraction_units[{index}] 既没有自有标题，也没有可继承的父标题"
        for index, unit in enumerate(units, start=1)
        if not unit.item_heading and not unit.parent_heading
    ]


def is_numbered_marker(marker: str | None) -> bool:
    return bool(marker and NUMBERED_MARKER_PATTERN.fullmatch(marker.strip()))


def combine_clause_number(parent: str | None, marker: str | None) -> str | None:
    """组合父级编号与编号型子标记；项目符号不伪造层级编号。"""

    if marker_repeats_parent_number(parent, marker):
        return parent
    if not is_numbered_marker(marker):
        return parent
    clean_marker = marker.strip()
    if parent is None:
        return clean_marker
    clean_parent = re.sub(r"[、.．]\s*$", "", parent.strip())
    # 2.1 这类完整层级号已经包含父编号，不能再次拼成 22.1。
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)+[.．]?", clean_marker):
        return clean_marker.rstrip(".．")
    if clean_marker.startswith(("（", "(")):
        return f"{clean_parent}{clean_marker}"
    child = re.sub(r"[、.．)]$", "", clean_marker)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", clean_parent) and child.isdigit():
        return f"{clean_parent}.{child}"
    if re.fullmatch(r"[一二三四五六七八九十百]+", clean_parent) and re.fullmatch(
        r"[一二三四五六七八九十百]+", child
    ):
        return f"{clean_parent}（{child}）"
    return f"{clean_parent}{clean_marker}"


def resolve_reviewed_units(
    units: list[ConsolidatedClauseCandidate],
) -> list[ResolvedClauseUnit]:
    """标题采用“子项自有标题优先，否则继承最近父标题”的唯一规则。"""

    resolved: list[ResolvedClauseUnit] = []
    resolution_errors = collect_resolution_errors(units)
    if resolution_errors:
        raise ValueError("；".join(resolution_errors))
    for unit in units:
        if unit.item_heading:
            heading = unit.item_heading
            heading_source: Literal["own", "inherited"] = "own"
        elif unit.item_marker is not None and unit.parent_heading:
            heading = unit.parent_heading
            heading_source = "inherited"
        elif unit.parent_heading:
            heading = unit.parent_heading
            heading_source = "own"
        else:  # pragma: no cover - 上方完整错误收集已阻止该分支。
            raise AssertionError("不可达的标题解析分支")
        resolved.append(
            ResolvedClauseUnit(
                source_candidate_indices=unit.source_candidate_indices,
                clause_number=combine_clause_number(
                    unit.parent_clause_number, unit.item_marker
                ),
                heading=heading,
                heading_source=heading_source,
                opening_anchor=unit.opening_anchor,
                end_before_anchor=unit.end_before_anchor,
                page_refs=unit.page_refs,
            )
        )
    return resolved


def validate_clause_unit_extraction(
    extraction: ClauseUnitExtraction,
    unit: ResolvedClauseUnit,
    source_page_count: int,
    *,
    unit_index: int,
) -> list[str]:
    clause = extraction.clauses[0]
    location = f"unit[{unit_index}].clauses[1]"
    errors = validate_page_refs(clause.page_refs, source_page_count, location=location)
    if clause.clause_number != unit.clause_number:
        errors.append(f"{location}: clause_number 必须与程序解析的提取单元一致")
    if clause.heading != unit.heading:
        errors.append(f"{location}: heading 必须与程序解析的提取单元一致")
    compact_source = re.sub(r"\s+", "", clause.source_text)
    compact_opening = re.sub(r"\s+", "", unit.opening_anchor)
    if compact_opening not in compact_source:
        errors.append(f"{location}: source_text 必须包含当前单元的 opening_anchor")
    if unit.end_before_anchor:
        compact_end = re.sub(r"\s+", "", unit.end_before_anchor)
        if compact_end in compact_source:
            errors.append(
                f"{location}: source_text 不得包含 end_before_anchor 或其后内容"
            )
    # 仅规范化 heading 的比较形式；source_text 始终保持 PDF 原文。字符间允许出现任意
    # Unicode 空白，兼容“运 输”等视觉排版，同时仍要求标题字符顺序完整出现。
    compact_heading = re.sub(r"\s+", "", clause.heading)
    whitespace_tolerant_heading = (
        re.compile(r"\s*".join(re.escape(character) for character in compact_heading))
        if compact_heading
        else None
    )
    if (
        unit.heading_source == "own"
        and (
            whitespace_tolerant_heading is None
            or whitespace_tolerant_heading.search(clause.source_text) is None
        )
    ):
        errors.append(
            f"{location}: 自有 heading 必须按原字符顺序出现在 source_text 原文中，"
            "标题字符之间允许空白"
        )
    return errors


def render_yaml(content: Any) -> str:
    return yaml.safe_dump(
        content,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    ).strip()


def render_review_projections(projections: list[ClauseReviewProjection]) -> str:
    return render_yaml(
        {"candidates": [projection.model_dump(mode="json") for projection in projections]}
    )


def render_current_projection(projection: ClauseReviewProjection) -> str:
    return render_yaml(projection.model_dump(mode="json"))


def render_clause_units(units: list[ResolvedClauseUnit]) -> str:
    return render_yaml(
        {
            "extraction_units": [
                {"unit_index": index, **unit.model_dump(mode="json")}
                for index, unit in enumerate(units, start=1)
            ]
        }
    )


def render_current_unit(index: int, unit: ResolvedClauseUnit) -> str:
    return render_yaml({"unit_index": index, **unit.model_dump(mode="json")})


def verify_vllm_connection(client: OpenAI, base_url: str) -> None:
    try:
        client.models.list()
    except Exception as error:
        raise SystemExit(
            f"无法连接本地 vLLM：{base_url}。请确认服务已启动、端口和模型配置正确。"
        ) from error


def print_metrics(step_name: str, metrics: dict[str, Any], max_model_len: int) -> None:
    usage = metrics["usage"]
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    print(f"\n[{step_name}]")
    print(f"  prompt_tokens: {prompt_tokens if prompt_tokens is not None else '服务未返回'}")
    print(f"  cached_tokens: {cached_tokens if cached_tokens is not None else '服务未返回或未统计'}")
    print(f"  completion_tokens: {completion_tokens if completion_tokens is not None else '服务未返回'}")
    print(f"  total_tokens: {total_tokens if total_tokens is not None else '服务未返回'}")
    if total_tokens is not None:
        print(f"  remaining_context_tokens: {max_model_len - total_tokens}")
    print(f"  image_count / image_bytes: {metrics['image_count']} / {metrics['image_bytes']}")
    print(f"  elapsed_seconds: {metrics['elapsed_seconds']}")


def aggregate_unit_metrics(unit_records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "unit_count": len(unit_records),
        "successful_unit_count": sum(r["status"] == "succeeded" for r in unit_records),
        "failed_unit_count": sum(r["status"] == "failed" for r in unit_records),
        "extracted_clause_count": sum(int(r.get("clause_count") or 0) for r in unit_records),
        "aggregate_elapsed_seconds": round(
            sum(float(r.get("metrics", {}).get("elapsed_seconds") or 0) for r in unit_records), 3
        ),
        "aggregate_usage": {
            key: sum(int(r.get("metrics", {}).get("usage", {}).get(key) or 0) for r in unit_records)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "units": unit_records,
    }


def aggregate_review_metrics(review_records: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总逐候选 Step 2 调用，不混用 Step 3 单元计数语义。"""

    return {
        "candidate_count": len(review_records),
        "successful_candidate_count": sum(
            record["status"] == "succeeded" for record in review_records
        ),
        "failed_candidate_count": sum(
            record["status"] == "failed" for record in review_records
        ),
        "included_candidate_count": sum(
            record.get("decision") == "include" for record in review_records
        ),
        "excluded_candidate_count": sum(
            record.get("decision") == "exclude" for record in review_records
        ),
        "aggregate_elapsed_seconds": round(
            sum(
                float(record.get("metrics", {}).get("elapsed_seconds") or 0)
                for record in review_records
            ),
            3,
        ),
        "aggregate_usage": {
            key: sum(
                int(record.get("metrics", {}).get("usage", {}).get(key) or 0)
                for record in review_records
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "candidates": review_records,
    }


def find_exact_duplicate_clauses(clauses: list[ClauseItem]) -> list[dict[str, int]]:
    first_positions: dict[str, int] = {}
    duplicates: list[dict[str, int]] = []
    for index, clause in enumerate(clauses, start=1):
        key = json.dumps(clause.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        if key in first_positions:
            duplicates.append({"first_index": first_positions[key], "duplicate_index": index})
        else:
            first_positions[key] = index
    return duplicates


def normalize_source_text_for_comparison(value: str) -> str:
    """仅用于结果校验；最终 source_text 原样保留，不做任何清洗。"""

    return re.sub(r"\s+", "", value)


def find_duplicate_source_texts(clauses: list[ClauseItem]) -> list[dict[str, int]]:
    """标题或类别不同也不能掩盖同一段原文被重复抽取。"""

    first_positions: dict[str, int] = {}
    duplicates: list[dict[str, int]] = []
    for index, clause in enumerate(clauses, start=1):
        key = normalize_source_text_for_comparison(clause.source_text)
        if key in first_positions:
            duplicates.append(
                {"first_index": first_positions[key], "duplicate_index": index}
            )
        else:
            first_positions[key] = index
    return duplicates


def find_source_text_containments(
    clauses: list[ClauseItem], *, minimum_length: int = 12
) -> list[dict[str, int]]:
    """检测父条款与子条款同时抽取造成的原文包含；过短片段不参与以减少误报。"""

    normalized = [
        normalize_source_text_for_comparison(clause.source_text) for clause in clauses
    ]
    containments: list[dict[str, int]] = []
    for left_index, left in enumerate(normalized, start=1):
        for right_index in range(left_index + 1, len(normalized) + 1):
            right = normalized[right_index - 1]
            if left == right or min(len(left), len(right)) < minimum_length:
                continue
            if left in right:
                containments.append(
                    {"container_index": right_index, "contained_index": left_index}
                )
            elif right in left:
                containments.append(
                    {"container_index": left_index, "contained_index": right_index}
                )
    return containments


def find_duplicate_clause_numbers(clauses: list[ClauseItem]) -> list[dict[str, Any]]:
    """同一非空引用编号出现多次通常意味着边界拆错或编号拼接错误。"""

    positions: dict[str, list[int]] = {}
    for index, clause in enumerate(clauses, start=1):
        if clause.clause_number is not None:
            positions.setdefault(clause.clause_number, []).append(index)
    return [
        {"clause_number": number, "indices": indices}
        for number, indices in positions.items()
        if len(indices) > 1
    ]


def replace_once(template: str, placeholder: str, value: str, *, step: str) -> str:
    if template.count(placeholder) != 1:
        raise ValueError(f"{step} 提示词必须恰好包含一个 {placeholder} 占位符。")
    return template.replace(placeholder, value)


def main(default_pdf_path: Path | None = None) -> None:
    root = project_root()
    args = parse_args(root)
    settings = load_settings(root)
    mllm = settings["models"]["mllm"]
    requested_pdf_path = args.pdf or default_pdf_path
    if requested_pdf_path is None:
        raise SystemExit("请在 DEFAULT_PDF_PATH 中设置路径，或通过 --pdf 传入 PDF。")
    pdf_path = resolve_path(requested_pdf_path, root)
    if not pdf_path.is_file():
        raise SystemExit(f"找不到待分析 PDF：{pdf_path}")

    max_pages = args.max_pages or mllm["vision"]["max_pages_per_request"]
    max_model_len = args.max_model_len or mllm["context_window_tokens"]
    if max_pages < 1 or max_model_len < 1:
        raise SystemExit("页面上限和上下文长度必须大于 0。")
    images, source_page_count = render_entire_pdf_as_data_urls(pdf_path, max_pages)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = resolve_path(args.output_dir, root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    prompt_dir = root / "experiments/clause_extraction/prompts"
    common_prefix = read_text(root / "experiments/prompts/00_contract_pdf_common_prefix.txt")
    page_context = build_page_visibility_context(source_page_count)
    step1_task = replace_once(
        read_text(prompt_dir / "01_discover_clause_structure.txt"),
        "{{PAGE_VISIBILITY_CONTEXT}}",
        page_context,
        step="Step 1",
    )
    step1b_template = replace_once(
        read_text(prompt_dir / "01b_consolidate_clause_boundaries.txt"),
        "{{PAGE_VISIBILITY_CONTEXT}}",
        page_context,
        step="Step 1B",
    )
    step2_template = replace_once(
        read_text(prompt_dir / "02_review_clause_candidates.txt"),
        "{{PAGE_VISIBILITY_CONTEXT}}",
        page_context,
        step="Step 2",
    )
    step3_template = replace_once(
        read_text(prompt_dir / "03_extract_clause_unit.txt"),
        "{{PAGE_VISIBILITY_CONTEXT}}",
        page_context,
        step="Step 3",
    )
    clause_spec = yaml.safe_load(
        (root / "description/fields/clause/clause.yaml").read_text(encoding="utf-8")
    )
    categories = clause_spec["output"]["items"]["properties"]["category"]["recommended_values"]
    step3_template = replace_once(
        step3_template, "{{CATEGORY_VALUES}}", "、".join(categories), step="Step 3"
    )

    write_json(
        run_dir / "run_manifest.json",
        {
            "run_id": run_id,
            "pdf_path": str(pdf_path),
            "pdf_name": pdf_path.name,
            "model": mllm["model"],
            "base_url": mllm["base_url"],
            "context_window_tokens": max_model_len,
            "source_pdf_page_count": source_page_count,
            "rendered_pages": [image["page"] for image in images],
            "whole_pdf_per_request": True,
            "stable_prefix_layout": "shared_text_then_all_images_then_task",
            "clause_schema_version": clause_spec.get("schema_version"),
            "execution_mode": "discover_consolidate_review_extract",
            "core_context_in_clause_requests": False,
            "step1_explicit_structure_normalization": True,
            "step1b_boundary_mode": "global_index_only_non_overlapping_groups",
            "step2_review_mode": "per_consolidated_group_fused_heading_and_boundaries",
            "step1_max_completion_tokens": min(
                mllm["generation"]["max_completion_tokens"],
                STEP_1_MAX_COMPLETION_TOKENS,
            ),
            "step1b_max_completion_tokens": min(
                mllm["generation"]["max_completion_tokens"],
                STEP_1B_MAX_COMPLETION_TOKENS,
            ),
            "step2_candidate_max_completion_tokens": min(
                mllm["generation"]["max_completion_tokens"],
                STEP_2_CANDIDATE_MAX_COMPLETION_TOKENS,
            ),
            "step3_unit_max_completion_tokens": min(
                mllm["generation"]["max_completion_tokens"],
                STEP_3_UNIT_MAX_COMPLETION_TOKENS,
            ),
        },
    )
    (run_dir / "00_common_prefix_prompt.txt").write_text(common_prefix, encoding="utf-8")
    (run_dir / "01_discover_clause_structure_task.txt").write_text(step1_task, encoding="utf-8")

    api_key = os.getenv(mllm["api_key_env"]) or "EMPTY"
    http_client = httpx.Client(timeout=mllm["timeout_seconds"], trust_env=False)
    client = OpenAI(base_url=mllm["base_url"], api_key=api_key, http_client=http_client)
    verify_vllm_connection(client, mllm["base_url"])
    print(f"已连接本地 vLLM：{mllm['base_url']}（模型：{mllm['model']}）")

    if args.print_prompts:
        print(f"\n===== 公共前缀 =====\n{common_prefix}")
        print(f"\n===== Step 1 =====\n{step1_task}")

    # Step 1 只做结构发现，不在首次视觉扫描时同时施加条款资格判断。
    structure_raw, step1_metrics, _ = invoke_json(
        client=client,
        model=mllm["model"],
        common_prefix=common_prefix,
        images=images,
        task_suffix=step1_task,
        schema_model=ClauseStructureMap,
        schema_name="clause_structure_map",
        generation=mllm["generation"],
        max_completion_tokens=min(
            mllm["generation"]["max_completion_tokens"],
            STEP_1_MAX_COMPLETION_TOKENS,
        ),
        raw_response_path=run_dir / "01_raw_response.json",
        metrics_path=run_dir / "01_metrics.json",
    )
    structure_map = ClauseStructureMap.model_validate(structure_raw)
    structure_errors = validate_structure_map(structure_map, source_page_count)
    write_json(run_dir / "01_structure_map.json", structure_map.model_dump(mode="json"))
    write_json(run_dir / "01_structure_map_validation.json", {"errors": structure_errors})
    if structure_errors:
        raise RuntimeError("Clause 结构地图业务校验失败：" + "；".join(structure_errors))
    normalized_structure_map, normalization_changes = normalize_structure_map(
        structure_map
    )
    write_json(
        run_dir / "01_normalized_structure_map.json",
        normalized_structure_map.model_dump(mode="json"),
    )
    write_json(
        run_dir / "01_structure_normalization.json",
        {
            "changed_candidate_count": len(normalization_changes),
            "changes": normalization_changes,
        },
    )
    print_metrics("Step 1: 穷举结构候选", step1_metrics, max_model_len)

    # Step 1B 全局核销 Step 1 原子，先消除标题/正文拆分及父子同时保留产生的重叠。
    step1b_task = replace_once(
        step1b_template,
        "{{STRUCTURE_MAP}}",
        render_indexed_structure_map(normalized_structure_map),
        step="Step 1B",
    )
    (run_dir / "01b_consolidate_clause_boundaries_task.txt").write_text(
        step1b_task, encoding="utf-8"
    )
    write_json(run_dir / "01b_boundary_schema.json", ClauseBoundaryPlan.model_json_schema())
    if args.print_prompts:
        print(f"\n===== Step 1B =====\n{step1b_task}")
    boundary_raw, step1b_metrics, _ = invoke_json(
        client=client,
        model=mllm["model"],
        common_prefix=common_prefix,
        images=images,
        task_suffix=step1b_task,
        schema_model=ClauseBoundaryPlan,
        schema_name="clause_boundary_plan",
        generation=mllm["generation"],
        max_completion_tokens=min(
            mllm["generation"]["max_completion_tokens"],
            STEP_1B_MAX_COMPLETION_TOKENS,
        ),
        raw_response_path=run_dir / "01b_raw_response.json",
        metrics_path=run_dir / "01b_metrics.json",
    )
    boundary_plan = ClauseBoundaryPlan.model_validate(boundary_raw)
    boundary_errors = validate_boundary_plan(boundary_plan, normalized_structure_map)
    write_json(
        run_dir / "01b_boundary_plan.json", boundary_plan.model_dump(mode="json")
    )
    write_json(
        run_dir / "01b_boundary_validation.json",
        {"errors": boundary_errors, "is_valid": not boundary_errors},
    )
    if boundary_errors:
        raise RuntimeError("Clause 边界计划业务校验失败：" + "；".join(boundary_errors))
    consolidated_candidates = build_consolidated_candidates(
        boundary_plan, normalized_structure_map
    )
    write_json(
        run_dir / "01b_clause_groups.json",
        {
            "candidates": [
                candidate.model_dump(mode="json")
                for candidate in consolidated_candidates
            ]
        },
    )
    print_metrics("Step 1B: 合并非重叠条款边界", step1b_metrics, max_model_len)

    # Step 2 只读取完整条款组的融合标题与硬边界，逐项判断，不再处理结构原子。
    review_projections = build_review_projections(consolidated_candidates)
    write_json(
        run_dir / "02_review_candidates.json",
        {"candidates": [item.model_dump(mode="json") for item in review_projections]},
    )
    step2_common = replace_once(
        step2_template,
        "{{REVIEW_CANDIDATES}}",
        render_review_projections(review_projections),
        step="Step 2",
    )
    if step2_common.count("{{CURRENT_CANDIDATE}}") != 1:
        raise ValueError("Step 2 提示词必须恰好包含一个 {{CURRENT_CANDIDATE}} 占位符。")
    step2_before_current, step2_after_current = step2_common.split(
        "{{CURRENT_CANDIDATE}}"
    )
    (run_dir / "02_review_common_suffix.txt").write_text(
        step2_before_current, encoding="utf-8"
    )
    write_json(
        run_dir / "02_review_schema.json",
        ClauseRetentionDecision.model_json_schema(),
    )
    review_root = run_dir / "02_reviews"
    review_root.mkdir()
    decisions: list[ClauseRetentionDecision | None] = []
    review_records: list[dict[str, Any]] = []

    for projection in review_projections:
        candidate_index = int(projection.candidate_index)
        candidate_dir = review_root / f"{candidate_index:03d}"
        candidate_dir.mkdir()
        current_candidate = render_current_projection(projection)
        task_suffix = step2_before_current + current_candidate + step2_after_current
        (candidate_dir / "current_candidate.yaml").write_text(
            current_candidate + "\n", encoding="utf-8"
        )
        if args.print_prompts:
            print(f"\n===== Step 2 候选 {candidate_index:03d} =====\n{task_suffix}")
        metrics_path = candidate_dir / "metrics.json"
        try:
            decision_raw, candidate_metrics, _ = invoke_json(
                client=client,
                model=mllm["model"],
                common_prefix=common_prefix,
                images=images,
                task_suffix=task_suffix,
                schema_model=ClauseRetentionDecision,
                schema_name=f"clause_retention_{candidate_index:03d}",
                generation=mllm["generation"],
                max_completion_tokens=min(
                    mllm["generation"]["max_completion_tokens"],
                    STEP_2_CANDIDATE_MAX_COMPLETION_TOKENS,
                ),
                raw_response_path=candidate_dir / "raw_response.json",
                metrics_path=metrics_path,
            )
            decision = ClauseRetentionDecision.model_validate(decision_raw)
            decision_errors = validate_retention_decision(projection, decision)
            if decision_errors:
                raise StructuredOutputError(
                    "模型响应未通过候选保留业务校验：" + "；".join(decision_errors),
                    finish_reason=None,
                )
        except StructuredOutputError as error:
            candidate_metrics = (
                json.loads(metrics_path.read_text(encoding="utf-8"))
                if metrics_path.is_file()
                else {}
            )
            failure = {
                "candidate_index": candidate_index,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "finish_reason": error.finish_reason,
                "metrics": candidate_metrics,
            }
            write_json(candidate_dir / "failure.json", failure)
            decisions.append(None)
            review_records.append(failure)
            print(f"\n[Step 2 候选 {candidate_index:03d}] 失败：{error}")
            continue
        write_json(candidate_dir / "decision.json", decision.model_dump(mode="json"))
        decisions.append(decision)
        record = {
            "candidate_index": candidate_index,
            "status": "succeeded",
            "fused_heading": projection.fused_heading,
            "location": projection.location,
            **decision.model_dump(mode="json"),
            "metrics": candidate_metrics,
        }
        review_records.append(record)
        print_metrics(
            f"Step 2 候选 {candidate_index:03d}", candidate_metrics, max_model_len
        )

    review_manifest = aggregate_review_metrics(review_records)
    failed_review_indices = [
        record["candidate_index"]
        for record in review_records
        if record["status"] == "failed"
    ]
    review_manifest["failed_candidate_indices"] = failed_review_indices
    write_json(run_dir / "02_review_manifest.json", review_manifest)
    write_json(run_dir / "02_clause_review.json", {"candidate_reviews": review_records})
    selected_candidates = select_reviewed_candidates(
        consolidated_candidates, decisions
    )
    resolution_errors = collect_resolution_errors(selected_candidates)
    write_json(
        run_dir / "02_clause_review_validation.json",
        {
            "failed_candidate_indices": failed_review_indices,
            "resolution_errors": resolution_errors,
            "is_complete": not failed_review_indices,
            "is_valid": not failed_review_indices and not resolution_errors,
        },
    )
    if resolution_errors:
        raise RuntimeError("Clause 候选标题解析失败：" + "；".join(resolution_errors))
    resolved_units = resolve_reviewed_units(selected_candidates)
    write_json(
        run_dir / "02_clause_units.json",
        {"extraction_units": [unit.model_dump(mode="json") for unit in resolved_units]},
    )

    rendered_units = render_clause_units(resolved_units)
    step3_common = replace_once(
        step3_template, "{{CLAUSE_UNITS}}", rendered_units, step="Step 3"
    )
    if step3_common.count("{{CURRENT_UNIT}}") != 1:
        raise ValueError("Step 3 提示词必须恰好包含一个 {{CURRENT_UNIT}} 占位符。")
    before_current, after_current = step3_common.split("{{CURRENT_UNIT}}")
    (run_dir / "03_extract_clause_common_suffix.txt").write_text(before_current, encoding="utf-8")
    write_json(run_dir / "03_clause_schema.json", ClauseUnitExtraction.model_json_schema())
    unit_root = run_dir / "03_units"
    unit_root.mkdir()
    merged_clauses: list[ClauseItem] = []
    unit_records: list[dict[str, Any]] = []

    # Step 3 一次只做一个已解析单元；失败隔离不会阻断后续条款。
    for unit_index, unit in enumerate(resolved_units, start=1):
        unit_dir = unit_root / f"{unit_index:03d}"
        unit_dir.mkdir()
        current_unit = render_current_unit(unit_index, unit)
        task_suffix = before_current + current_unit + after_current
        (unit_dir / "current_unit.yaml").write_text(current_unit + "\n", encoding="utf-8")
        if args.print_prompts:
            print(f"\n===== Step 3 单元 {unit_index:03d} =====\n{task_suffix}")
        metrics_path = unit_dir / "metrics.json"
        try:
            extracted_raw, unit_metrics, _ = invoke_json(
                client=client,
                model=mllm["model"],
                common_prefix=common_prefix,
                images=images,
                task_suffix=task_suffix,
                schema_model=ClauseUnitExtraction,
                schema_name=f"clause_unit_{unit_index:03d}",
                generation=mllm["generation"],
                max_completion_tokens=min(
                    mllm["generation"]["max_completion_tokens"],
                    STEP_3_UNIT_MAX_COMPLETION_TOKENS,
                ),
                raw_response_path=unit_dir / "raw_response.json",
                metrics_path=metrics_path,
            )
            unit_extraction = ClauseUnitExtraction.model_validate(extracted_raw)
            unit_errors = validate_clause_unit_extraction(
                unit_extraction, unit, source_page_count, unit_index=unit_index
            )
            if unit_errors:
                raise StructuredOutputError(
                    "模型响应未通过 Clause 业务校验：" + "；".join(unit_errors),
                    finish_reason=None,
                )
        except StructuredOutputError as error:
            unit_metrics = (
                json.loads(metrics_path.read_text(encoding="utf-8"))
                if metrics_path.is_file()
                else {}
            )
            failure = {
                "unit_index": unit_index,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "finish_reason": error.finish_reason,
                "metrics": unit_metrics,
            }
            write_json(unit_dir / "failure.json", failure)
            unit_records.append(failure)
            print(f"\n[Step 3 单元 {unit_index:03d}] 失败：{error}")
            continue
        write_json(unit_dir / "extraction.json", unit_extraction.model_dump(mode="json"))
        merged_clauses.extend(unit_extraction.clauses)
        record = {
            "unit_index": unit_index,
            "status": "succeeded",
            "clause_count": 1,
            "metrics": unit_metrics,
        }
        unit_records.append(record)
        print_metrics(f"Step 3 单元 {unit_index:03d}", unit_metrics, max_model_len)

    unit_manifest = aggregate_unit_metrics(unit_records)
    failed_units = [r["unit_index"] for r in unit_records if r["status"] == "failed"]
    unit_manifest["failed_unit_indices"] = failed_units
    write_json(run_dir / "03_unit_manifest.json", unit_manifest)
    extraction = ClauseExtraction(clauses=merged_clauses)
    duplicates = find_exact_duplicate_clauses(extraction.clauses)
    duplicate_sources = find_duplicate_source_texts(extraction.clauses)
    source_containments = find_source_text_containments(extraction.clauses)
    duplicate_numbers = find_duplicate_clause_numbers(extraction.clauses)
    validation = {
        "failed_review_candidate_indices": failed_review_indices,
        "failed_unit_indices": failed_units,
        "exact_duplicate_clauses": duplicates,
        "duplicate_source_texts": duplicate_sources,
        "source_text_containments": source_containments,
        "duplicate_clause_numbers": duplicate_numbers,
        "is_complete": not failed_review_indices and not failed_units,
        "is_valid": not failed_review_indices
        and not failed_units
        and not duplicates
        and not duplicate_sources
        and not source_containments
        and not duplicate_numbers,
    }
    write_json(run_dir / "03_clause_extraction.json", extraction.model_dump(mode="json"))
    write_json(run_dir / "clause_validation.json", validation)
    write_json(run_dir / "final_clauses.json", extraction.model_dump(mode="json"))
    write_json(
        run_dir / "metrics.json",
        {
            "max_model_len": max_model_len,
            "step_1": step1_metrics,
            "step_1b": step1b_metrics,
            "step_2": review_manifest,
            "step_3": unit_manifest,
        },
    )
    print("\n最终 Clause 提取结果：")
    print(json.dumps(extraction.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not validation["is_valid"]:
        print(
            "\n警告：存在阶段失败、原文重复/包含或编号重复，"
            "详见 clause_validation.json。"
        )
    print(f"\n完整实验产物已写入：{run_dir}")


if __name__ == "__main__":
    DEFAULT_PDF_PATH = Path("data/input/ET-3030加热台合同2025-04-03_已签章.pdf")
    try:
        main(default_pdf_path=DEFAULT_PDF_PATH)
    except SystemExit:
        raise
    except Exception as error:
        print(f"实验失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
