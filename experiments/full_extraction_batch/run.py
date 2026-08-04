#!/usr/bin/env python3
"""完整生产提取并行图的分文件 YAML 批量功能实验。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import re
import sys
import time
from typing import Any, Awaitable, Callable, Sequence

import httpx
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contract_processor.application.errors import StageValidationError  # noqa: E402
from contract_processor.application.workflows.contract_processing import (  # noqa: E402
    ContractExtractionPipelines,
)
from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.bootstrap.container import build_process_contract  # noqa: E402
from contract_processor.settings import load_project_settings  # noqa: E402
DEFAULT_INPUT_DIR = Path("data/input")
DEFAULT_OUTPUT_ROOT = Path("experiments/outputs/full_extraction_batch")


def _write_yaml_sync(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False, width=120)


def _safe_stem(source_name: str, *, limit: int = 80) -> str:
    stem = Path(source_name).stem.strip()
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", stem)
    return normalized[:limit] or "contract"


def _parse_prometheus_cache_counters(text: str) -> dict[str, Any]:
    query_values: list[float] = []
    hit_values: list[float] = []
    gauge_values: list[float] = []
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
        elif "prefix_cache_hit_rate" in lowered:
            gauge_values.append(value)
            metric_names.add(name)
    return {
        "query_counter": sum(query_values) if query_values else None,
        "hit_counter": sum(hit_values) if hit_values else None,
        "reported_hit_rate": gauge_values[-1] if gauge_values else None,
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
    """用累计计数差计算单份合同完整图的缓存命中率。"""

    base = {
        "metric": "vllm_prefix_cache",
        "measurement_scope": "vllm_instance_counter_delta_during_file",
        "metrics_url": after.get("metrics_url") or before.get("metrics_url"),
    }
    if not before.get("available") or not after.get("available"):
        return {
            **base,
            "status": "unavailable",
            "reason": "处理前或处理后的 vLLM /metrics 快照不可用。",
            "before": before,
            "after": after,
        }
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
            "reported_global_hit_rate_after_file": after.get("reported_hit_rate"),
            "metric_names": after.get("metric_names", []),
        }
    query_delta = float(values[1]) - float(values[0])
    hit_delta = float(values[3]) - float(values[2])
    if query_delta <= 0 or hit_delta < 0:
        return {
            **base,
            "status": "unavailable",
            "reason": "缓存累计计数没有增长或在文件窗口内发生重置。",
            "query_delta": query_delta,
            "hit_delta": hit_delta,
        }
    return {
        **base,
        "status": "measured",
        "aggregation": "weighted_average_across_nodes_by_cache_queries",
        "query_delta": query_delta,
        "hit_delta": hit_delta,
        "average_hit_rate": round(hit_delta / query_delta, 6),
        "average_hit_rate_percent": round(hit_delta / query_delta * 100, 2),
        "metric_names": sorted(
            set(before.get("metric_names", [])) | set(after.get("metric_names", []))
        ),
        "caveat": "同一 vLLM 若同时服务其他请求，计数差会包含外部流量。",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批量运行 Core/Attribute/Clause/Abstract 完整并行提取图"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="待测 PDF 目录，相对路径以项目根目录为准。",
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


class TimedExtractionPipelines:
    """透明代理正式抽取端口，并记录各并行节点的真实时间窗口。"""

    def __init__(self, wrapped: ContractExtractionPipelines) -> None:
        self._wrapped = wrapped
        self._origin = time.perf_counter()
        self.timings: dict[str, dict[str, Any]] = {}

    @property
    def model_name(self) -> str:
        return self._wrapped.model_name

    @property
    def prompt_version(self) -> str:
        return self._wrapped.prompt_version

    @property
    def attribute_catalog_mode(self) -> str:
        return self._wrapped.attribute_catalog_mode

    @property
    def attribute_extraction_metrics(self) -> dict[str, Any]:
        return getattr(self._wrapped, "attribute_extraction_metrics", {})

    async def _measure(
        self, stage: str, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        status = "succeeded"
        try:
            return await operation()
        except Exception:
            status = "failed"
            raise
        finally:
            completed_at = datetime.now(UTC)
            self.timings[stage] = {
                "status": status,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "start_offset_seconds": round(started - self._origin, 3),
                "wall_clock_seconds": round(time.perf_counter() - started, 3),
            }

    async def prepare(self, pdf_path: Path) -> dict[str, Any]:
        return await self._measure("prepare", lambda: self._wrapped.prepare(pdf_path))

    async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
        return await self._measure(
            "core", lambda: self._wrapped.extract_core(pdf_path)
        )

    async def extract_attributes(self, core: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._measure(
            "attribute", lambda: self._wrapped.extract_attributes(core)
        )

    async def extract_clauses(self, pdf_path: Path) -> dict[str, Any]:
        return await self._measure(
            "clause", lambda: self._wrapped.extract_clauses(pdf_path)
        )

    async def extract_abstract(self, pdf_path: Path) -> dict[str, Any]:
        return await self._measure(
            "abstract", lambda: self._wrapped.extract_abstract(pdf_path)
        )

    async def schema_versions(self) -> dict[str, str]:
        return await self._wrapped.schema_versions()

    async def close(self) -> None:
        await self._wrapped.close()


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _result_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "core_field_count": len(payload["core"]),
        "attribute_field_count": len(payload["attribute"]),
        "clause_count": len(payload["clause"]),
        "abstract_section_count": len(payload["abstract"]["sections"]),
        "abstract_character_count": len(payload["abstract"]["text"].strip()),
    }


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

    settings = await load_project_settings(PROJECT_ROOT)
    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_root / run_id
    contract_dir = run_dir / "contracts"
    await run_blocking(contract_dir.mkdir, parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "experiment": "full_extraction_batch",
        "run_id": run_id,
        "status": "running",
        "started_at": started_at.isoformat(),
        "input": {
            "directory": str(input_dir),
            "contracts": [path.name for path in pdf_paths],
        },
        "runtime": {
            "mode": "production",
            "model": settings.models.mllm.model,
            "max_concurrent_requests": settings.models.mllm.max_concurrent_requests,
            "graph": (
                "prepare -> [core -> attribute, clause, abstract] -> finalize"
            ),
            "batch_policy": "合同串行；单合同内部使用正式 LangGraph 并行拓扑",
            "cache_measurement": "每份合同完整图执行前后读取 vLLM /metrics 计数差",
        },
    }
    await run_blocking(_write_yaml_sync, run_dir / "manifest.yaml", manifest)

    summaries: list[dict[str, Any]] = []
    batch_started = time.perf_counter()
    for index, pdf_path in enumerate(pdf_paths, start=1):
        print(f"[{index:02d}/{len(pdf_paths):02d}] {pdf_path.name} 开始", flush=True)
        file_started_at = datetime.now(UTC)
        file_started = time.perf_counter()
        cache_before = await _snapshot_vllm_cache(settings.models.mllm.base_url)
        timed_holder: dict[str, TimedExtractionPipelines] = {}

        def observe(pipelines: ContractExtractionPipelines) -> ContractExtractionPipelines:
            timed = TimedExtractionPipelines(pipelines)
            timed_holder["pipelines"] = timed
            return timed

        report: dict[str, Any] = {
            "source_name": pdf_path.name,
            "started_at": file_started_at.isoformat(),
        }
        try:
            use_case = await build_process_contract(
                PROJECT_ROOT, pipelines_transform=observe
            )
            result = await use_case.execute(pdf_path)
            payload = result.model_dump(mode="json", exclude={"document_id"})
            attribute_status = payload["processing"]["attribute_extraction"][
                "status"
            ]
            report.update(
                {
                    "status": "succeeded",
                    "attribute_extraction_status": attribute_status,
                    "processing": payload["processing"],
                    "counts": _result_counts(payload),
                    "core": payload["core"],
                    "attribute": payload["attribute"],
                    "clause": payload["clause"],
                    "abstract": payload["abstract"],
                }
            )
        except StageValidationError as error:
            report.update(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "failure": {
                        "stage": error.stage,
                        "validation": error.validation,
                        "metrics": error.metrics,
                    },
                }
            )
        except Exception as error:
            report.update(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": (str(error).strip() or type(error).__name__)[:1200],
                }
            )
        cache_after = await _snapshot_vllm_cache(settings.models.mllm.base_url)
        report["completed_at"] = datetime.now(UTC).isoformat()
        timed_pipelines = timed_holder.get("pipelines")
        report["timing"] = {
            "wall_clock_seconds": round(time.perf_counter() - file_started, 3),
            "stages": timed_pipelines.timings if timed_pipelines is not None else {},
        }
        report["cache"] = calculate_file_cache_metrics(cache_before, cache_after)
        result_name = f"{index:02d}_{_safe_stem(pdf_path.name)}.yaml"
        await run_blocking(_write_yaml_sync, contract_dir / result_name, report)
        summary = {
            key: value
            for key, value in report.items()
            if key not in {"core", "attribute", "clause", "abstract", "failure"}
        }
        summary["result_file"] = f"contracts/{result_name}"
        summaries.append(summary)
        await run_blocking(
            _write_yaml_sync,
            run_dir / "summary.yaml",
            {
                "experiment": "full_extraction_batch",
                "run_id": run_id,
                "status": "running",
                "contracts": summaries,
            },
        )
        print(
            f"[{index:02d}/{len(pdf_paths):02d}] {pdf_path.name} {report['status']}："
            f"wall={report['timing']['wall_clock_seconds']}s，"
            f"cache_avg={report['cache'].get('average_hit_rate_percent', 'N/A')}%",
            flush=True,
        )

    completed_at = datetime.now(UTC)
    succeeded = sum(item["status"] == "succeeded" for item in summaries)
    failed = len(summaries) - succeeded
    partial_attribute = sum(
        item.get("attribute_extraction_status") == "completed_with_failures"
        for item in summaries
    )
    measured_cache = [
        item["cache"]
        for item in summaries
        if item["cache"].get("status") == "measured"
    ]
    total_queries = sum(item["query_delta"] for item in measured_cache)
    total_hits = sum(item["hit_delta"] for item in measured_cache)
    summary = {
        "experiment": "full_extraction_batch",
        "run_id": run_id,
        "status": (
            "completed_with_failures" if failed or partial_attribute else "completed"
        ),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "batch": {
            "contract_count": len(pdf_paths),
            "succeeded_contract_count": succeeded,
            "failed_contract_count": failed,
            "partial_attribute_contract_count": partial_attribute,
            "wall_clock_seconds": round(time.perf_counter() - batch_started, 3),
            "average_contract_seconds": round(
                sum(item["timing"]["wall_clock_seconds"] for item in summaries)
                / len(summaries),
                3,
            ),
            "weighted_average_cache_hit_rate_percent": (
                round(total_hits / total_queries * 100, 2)
                if total_queries
                else None
            ),
        },
        "contracts": summaries,
    }
    await run_blocking(_write_yaml_sync, run_dir / "summary.yaml", summary)
    manifest.update(
        {
            "status": summary["status"],
            "completed_at": completed_at.isoformat(),
            "outputs": {
                "summary": "summary.yaml",
                "contracts": "contracts/*.yaml",
            },
        }
    )
    await run_blocking(_write_yaml_sync, run_dir / "manifest.yaml", manifest)
    return run_dir, summary


async def async_main(argv: Sequence[str] | None = None) -> tuple[Path, dict[str, Any]]:
    return await run_experiment(parse_args(argv))


def main(argv: Sequence[str] | None = None) -> int:
    run_dir, summary = asyncio.run(async_main(argv))
    print(f"实验产物：{run_dir}")
    return 1 if summary["status"] == "completed_with_failures" else 0


if __name__ == "__main__":
    # IDE 手动启动时只修改这里；终端显式参数始终优先。
    IDE_INPUT_DIR = "data/input"
    IDE_OUTPUT_DIR = "experiments/outputs/full_extraction_batch"
    IDE_MAX_DOCUMENTS: int | None = None
    resolved_argv = sys.argv[1:] or build_ide_argv(
        input_dir=IDE_INPUT_DIR,
        output_dir=IDE_OUTPUT_DIR,
        max_documents=IDE_MAX_DOCUMENTS,
    )
    raise SystemExit(main(resolved_argv))
