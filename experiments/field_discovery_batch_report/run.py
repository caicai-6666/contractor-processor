#!/usr/bin/env python3
"""字段发现正式批次的精简 YAML 汇报实验。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sys
import time
from typing import Any, Sequence

import httpx
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.bootstrap.container import (  # noqa: E402
    build_discover_fields_from_batch,
)
from contract_processor.settings import load_project_settings  # noqa: E402
DEFAULT_INPUT_DIR = Path("data/input")
DEFAULT_OUTPUT_ROOT = Path("experiments/outputs/field_discovery_batch_report")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行字段发现批次并生成精简 YAML 汇报")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="待发现字段的 PDF 目录，相对路径以项目根目录为准。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="实验输出根目录，相对路径以项目根目录为准。",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=None,
        help="仅运行排序后的前 N 份合同；省略时运行全部合同。",
    )
    return parser.parse_args(argv)


def build_ide_argv(
    *, input_dir: str, output_dir: str, max_documents: int | None
) -> list[str]:
    argv = ["--input-dir", input_dir, "--output-dir", output_dir]
    if max_documents is not None:
        argv.extend(["--max-documents", str(max_documents)])
    return argv


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _write_yaml_sync(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False, width=120)


def _parse_prometheus_cache_counters(text: str) -> dict[str, Any]:
    query_values: list[float] = []
    hit_values: list[float] = []
    metric_names: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        sample, separator, raw_value = line.rpartition(" ")
        if not separator:
            continue
        name = sample.split("{", 1)[0]
        lowered = name.casefold()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if "prefix_cache_queries" in lowered and not lowered.endswith(("_created", "_bucket")):
            query_values.append(value)
            metric_names.add(name)
        elif "prefix_cache_hits" in lowered and not lowered.endswith(("_created", "_bucket")):
            hit_values.append(value)
            metric_names.add(name)
    return {
        "query_counter": sum(query_values) if query_values else None,
        "hit_counter": sum(hit_values) if hit_values else None,
        "metric_names": sorted(metric_names),
    }


async def _snapshot_vllm_cache(base_url: str) -> dict[str, Any]:
    metrics_url = base_url.rstrip("/")
    if metrics_url.endswith("/v1"):
        metrics_url = metrics_url[:-3]
    metrics_url += "/metrics"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(metrics_url)
            response.raise_for_status()
    except Exception as error:
        return {
            "available": False,
            "metrics_url": metrics_url,
            "error_type": type(error).__name__,
            "error": (str(error).strip() or type(error).__name__)[:300],
        }
    return {
        "available": True,
        "metrics_url": metrics_url,
        **_parse_prometheus_cache_counters(response.text),
    }


def calculate_file_cache_metrics(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """用同一 vLLM 实例的累计计数差计算批运行缓存命中率。"""

    base = {
        "metric": "vllm_prefix_cache",
        "measurement_scope": "vllm_instance_counter_delta_during_batch",
        "metrics_url": after.get("metrics_url") or before.get("metrics_url"),
    }
    if not before.get("available") or not after.get("available"):
        return {**base, "status": "unavailable", "before": before, "after": after}
    values = (
        before.get("query_counter"),
        after.get("query_counter"),
        before.get("hit_counter"),
        after.get("hit_counter"),
    )
    if not all(isinstance(value, (int, float)) for value in values):
        return {
            **base,
            "status": "unavailable",
            "reason": "vLLM 未暴露 prefix cache queries/hits 累计计数。",
        }
    query_delta = float(values[1]) - float(values[0])
    hit_delta = float(values[3]) - float(values[2])
    if query_delta <= 0 or hit_delta < 0:
        return {
            **base,
            "status": "unavailable",
            "reason": "缓存累计计数没有增长或在批次窗口内发生重置。",
        }
    return {
        **base,
        "status": "measured",
        "query_delta": query_delta,
        "hit_delta": hit_delta,
        "average_hit_rate": round(hit_delta / query_delta, 6),
        "average_hit_rate_percent": round(hit_delta / query_delta * 100, 2),
        "metric_names": sorted(
            set(before.get("metric_names", [])) | set(after.get("metric_names", []))
        ),
        "caveat": "同一 vLLM 若同时服务其他请求，计数差会包含外部流量。",
    }


def build_field_rows(
    *,
    frozen_candidates: Sequence[dict[str, Any]],
    statistics: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把正式两阶段结果投影为字段定义与人类可读命中率。"""

    statistics_by_ref = {item["candidate_ref"]: item for item in statistics}
    rows: list[dict[str, Any]] = []
    for candidate in frozen_candidates:
        candidate_ref = candidate["candidate_ref"]
        definition = candidate["definition"]
        item = statistics_by_ref[candidate_ref]
        found_source_names = [
            observation["source_name"]
            for observation in observations
            if observation["candidate_ref"] == candidate_ref
            and observation["task_status"] == "succeeded"
            and observation["extraction"]["status"] == "found"
        ]
        rows.append(
            {
                **definition,
                "statistics": {
                    "found_document_count": item["found_document_count"],
                    "document_count": item["document_count"],
                    # 汇报采用固定合同总数作为分母；技术失败不会抬高命中率。
                    "hit_rate": item["conservative_frequency"],
                    "hit_rate_percent": round(
                        float(item["conservative_frequency"]) * 100, 2
                    ),
                    "found_source_names": found_source_names,
                    "failed_document_count": item["failed_document_count"],
                },
            }
        )
    return rows


async def run_experiment(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    input_dir = await run_blocking(_project_path(args.input_dir).resolve)
    output_root = await run_blocking(_project_path(args.output_dir).resolve)
    if args.max_documents is not None and args.max_documents < 1:
        raise ValueError("--max-documents 必须大于 0。")
    pdf_paths = await run_blocking(lambda: sorted(input_dir.glob("*.pdf")))
    if args.max_documents is not None:
        pdf_paths = pdf_paths[: args.max_documents]
    if not pdf_paths:
        raise FileNotFoundError(f"合同目录中没有 PDF：{input_dir}")

    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_root / run_id
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)
    settings = await load_project_settings(PROJECT_ROOT)
    await run_blocking(
        _write_yaml_sync,
        run_dir / "status.yaml",
        {
            "experiment": "field_discovery_batch_report",
            "run_id": run_id,
            "status": "running",
            "started_at": started_at.isoformat(),
            "source_names": [path.name for path in pdf_paths],
        },
    )

    async def emit(message: str) -> None:
        print(message, flush=True)

    wall_started = time.perf_counter()
    cache_before = await _snapshot_vllm_cache(settings.models.mllm.base_url)
    try:
        use_case = await build_discover_fields_from_batch(
            PROJECT_ROOT, emit_progress=emit
        )
        result = await use_case.execute(pdf_paths)
    except asyncio.CancelledError:
        await run_blocking(
            _write_yaml_sync,
            run_dir / "status.yaml",
            {
                "experiment": "field_discovery_batch_report",
                "run_id": run_id,
                "status": "interrupted",
                "interrupted_at": datetime.now(UTC).isoformat(),
            },
        )
        raise
    except Exception as error:
        await run_blocking(
            _write_yaml_sync,
            run_dir / "status.yaml",
            {
                "experiment": "field_discovery_batch_report",
                "run_id": run_id,
                "status": "failed",
                "failed_at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__,
                "error": (str(error).strip() or type(error).__name__)[:1200],
            },
        )
        raise
    cache_after = await _snapshot_vllm_cache(settings.models.mllm.base_url)
    completed_at = datetime.now(UTC)
    cache = calculate_file_cache_metrics(cache_before, cache_after)
    cache["measurement_scope"] = "vllm_instance_counter_delta_during_batch"
    payload = {
        "experiment": "field_discovery_batch_report",
        "run_id": run_id,
        "status": (
            "completed_with_failures"
            if result.stage_one.status == "completed_with_failures"
            or result.stage_two.status == "completed_with_failures"
            else "completed"
        ),
        "batch": {
            "discovery_batch_id": result.batch_id,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "input_document_count": len(pdf_paths),
            "distinct_document_count": result.stage_two.document_count,
            "source_names": [path.name for path in pdf_paths],
            "wall_clock_seconds": round(time.perf_counter() - wall_started, 3),
            "average_cache_hit_rate": cache.get("average_hit_rate"),
            "average_cache_hit_rate_percent": cache.get(
                "average_hit_rate_percent"
            ),
            "cache_measurement": cache,
            "model": result.processing.model,
            "prompt_version": result.processing.prompt_version,
        },
        "summary": {
            "succeeded_document_count": result.stage_one.succeeded_document_count,
            "failed_document_count": result.stage_one.failed_document_count,
            "partial_attribute_document_count": (
                result.stage_one.partial_attribute_document_count
            ),
            # 该数值是通过单文档语义准入后的候选观测总数，并非模型原始生成条数。
            "accepted_candidate_observation_count": result.stage_one.raw_candidate_count,
            "candidate_identity_count": result.stage_one.candidate_identity_count,
            "group_count": result.stage_one.source_group_count,
            "discovered_field_count": len(result.stage_one.frozen_candidates),
            "extraction_task_count": result.stage_two.task_count,
            "failed_extraction_task_count": result.stage_two.failed_task_count,
        },
        "stage_one_failures": {
            "failed_documents": result.stage_one.failed_documents,
            "partial_attribute_documents": (
                result.stage_one.partial_attribute_documents
            ),
        },
        "fields": build_field_rows(
            frozen_candidates=result.stage_one.frozen_candidates,
            statistics=[item.model_dump(mode="json") for item in result.stage_two.statistics],
            observations=[
                item.model_dump(mode="json") for item in result.stage_two.observations
            ],
        ),
    }
    await run_blocking(_write_yaml_sync, run_dir / "result.yaml", payload)
    await run_blocking(
        _write_yaml_sync,
        run_dir / "status.yaml",
        {
            "experiment": "field_discovery_batch_report",
            "run_id": run_id,
            "status": payload["status"],
            "completed_at": completed_at.isoformat(),
            "result": "result.yaml",
        },
    )
    return run_dir, payload


async def async_main(argv: Sequence[str] | None = None) -> tuple[Path, dict[str, Any]]:
    return await run_experiment(parse_args(argv))


def main(argv: Sequence[str] | None = None) -> int:
    run_dir, payload = asyncio.run(async_main(argv))
    print(f"实验结果：{run_dir / 'result.yaml'}")
    return 1 if payload["status"] == "completed_with_failures" else 0


if __name__ == "__main__":
    # IDE 手动启动只修改这里；显式命令行参数始终优先。
    IDE_INPUT_DIR = "data/input"
    IDE_OUTPUT_DIR = "experiments/outputs/field_discovery_batch_report"
    IDE_MAX_DOCUMENTS: int | None = None
    resolved_argv = sys.argv[1:] or build_ide_argv(
        input_dir=IDE_INPUT_DIR,
        output_dir=IDE_OUTPUT_DIR,
        max_documents=IDE_MAX_DOCUMENTS,
    )
    raise SystemExit(main(resolved_argv))
