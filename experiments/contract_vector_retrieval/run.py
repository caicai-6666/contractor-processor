#!/usr/bin/env python3
"""完整提取后，在内存中评估合同四向量召回效果。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from dotenv import load_dotenv
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from contract_processor.application.schemas.contract_processing import (  # noqa: E402
    ContractProcessingResult,
)
from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.bootstrap.container import build_process_contract  # noqa: E402
from contract_processor.infrastructure.embedding import (  # noqa: E402
    Qwen3VLEmbeddingClient,
    load_contract_embedding_policy,
)
from contract_processor.settings import load_project_settings  # noqa: E402
from experiments.contract_vector_retrieval.evaluation import (  # noqa: E402
    evaluate_query,
    summarize_results,
)
from experiments.contract_vector_retrieval.transformations import (  # noqa: E402
    transform_pdf_sync,
)


DEFAULT_INPUT_DIR = Path("data/input")
DEFAULT_CASES = Path("experiments/contract_vector_retrieval/cases.yaml")
DEFAULT_OUTPUT_ROOT = Path("experiments/outputs/contract_vector_retrieval")
VECTOR_FIELDS = (
    "abstract_vector",
    "document_visual_vector",
    "contract_name_vector",
    "product_names_vector",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="完整提取合同，并以纯内存余弦排名评估四类向量召回"
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-documents", type=int, default=None)
    parser.add_argument(
        "--reuse-extraction-run",
        type=Path,
        default=None,
        help="复用指定失败运行的 extraction/*.yaml，仅重跑向量与召回阶段。",
    )
    return parser.parse_args(argv)


def build_ide_argv(
    *,
    input_dir: str,
    cases: str,
    output_dir: str,
    max_documents: int | None,
    reuse_extraction_run: str | None,
) -> list[str]:
    argv = ["--input-dir", input_dir, "--cases", cases, "--output-dir", output_dir]
    if max_documents is not None:
        argv.extend(["--max-documents", str(max_documents)])
    if reuse_extraction_run:
        argv.extend(["--reuse-extraction-run", reuse_extraction_run])
    return argv


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _write_yaml_sync(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(
            dict(payload), allow_unicode=True, sort_keys=False, width=120
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_cases_sync(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("contracts"), dict):
        raise ValueError("召回测试 cases.yaml 缺少 contracts 映射。")
    return payload


def _field_value(envelope: Any) -> Any:
    if not isinstance(envelope, dict) or envelope.get("status") != "found":
        return None
    return envelope.get("value")


def _vector_source_fields(result: ContractProcessingResult) -> dict[str, str]:
    """按正式入库投影语义提取三个文本向量的输入，不引入文件名信息。"""

    title = _field_value(result.core.get("contract_title"))
    subject = result.core.get("subject_matter")
    products: list[str] = []
    if isinstance(subject, dict):
        properties = subject.get("properties", {})
        items = _field_value(properties.get("items")) if isinstance(properties, dict) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("normalized_name") or item.get("source_name")
                if isinstance(name, str) and name.strip() and name.strip() not in products:
                    products.append(name.strip())
    sources = {
        "contract_name_vector": title.strip() if isinstance(title, str) else "",
        "product_names_vector": "；".join(products),
        "abstract_vector": result.abstract.text.strip(),
    }
    missing = [field for field, text in sources.items() if not text]
    if missing:
        raise ValueError(f"{result.source_name} 缺少可向量化字段：{missing}")
    return sources


def _load_extraction_results_sync(
    source_run: Path, pdf_paths: Sequence[Path]
) -> tuple[list[ContractProcessingResult], float | None]:
    """读取本实验自己保存且已通过正式 Schema 的提取结果。"""

    extraction_dir = source_run / "extraction"
    by_name: dict[str, ContractProcessingResult] = {}
    source_wall_times: list[float] = []
    for path in sorted(extraction_dir.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"提取产物不是对象：{path}")
        wall_time = payload.pop("experiment_wall_time_seconds", None)
        if isinstance(wall_time, (int, float)):
            source_wall_times.append(float(wall_time))
        payload.pop("reused_from", None)
        result = ContractProcessingResult.model_validate(payload)
        by_name[result.source_name] = result
    expected = [path.name for path in pdf_paths]
    if set(by_name) != set(expected):
        raise ValueError(
            "复用提取产物与当前输入不一致："
            f"缺少={sorted(set(expected) - set(by_name))}，"
            f"多余={sorted(set(by_name) - set(expected))}"
        )
    total_wall_time = (
        round(sum(source_wall_times), 3)
        if len(source_wall_times) == len(expected)
        else None
    )
    return [by_name[source_name] for source_name in expected], total_wall_time


async def _copy_reused_extractions(
    results: Sequence[ContractProcessingResult],
    extraction_dir: Path,
    source_run: Path,
) -> None:
    for index, result in enumerate(results, start=1):
        payload = result.model_dump(mode="json")
        payload["reused_from"] = str(source_run)
        await run_blocking(
            _write_yaml_sync,
            extraction_dir / f"{index:02d}_{Path(result.source_name).stem}.yaml",
            payload,
        )


def _validate_cases(cases: dict[str, Any], source_names: set[str]) -> None:
    configured = set(cases["contracts"])
    if configured != source_names:
        raise ValueError(
            "测试用例与输入合同集合不一致："
            f"缺少={sorted(source_names - configured)}，多余={sorted(configured - source_names)}"
        )


async def _build_embedding_client() -> tuple[Qwen3VLEmbeddingClient, Any, Any]:
    settings = await load_project_settings(PROJECT_ROOT)
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    embedding = settings.models.embedding
    policy_path = _resolve(settings.paths.contract_embedding_policy)
    policy = await load_contract_embedding_policy(policy_path)
    client = Qwen3VLEmbeddingClient(
        base_url=embedding.base_url,
        api_key=os.getenv(embedding.api_key_env) or "",
        model=embedding.model,
        endpoint=embedding.endpoint,
        timeout_seconds=embedding.timeout_seconds,
        dimensions=embedding.dimensions,
        max_concurrent_requests=embedding.max_concurrent_requests,
        normalize=embedding.normalize,
        policy=policy,
    )
    return client, embedding, policy


async def _extract_contracts(
    pdf_paths: Sequence[Path], extraction_dir: Path
) -> list[ContractProcessingResult]:
    results: list[ContractProcessingResult] = []
    for index, pdf_path in enumerate(pdf_paths, start=1):
        started = time.perf_counter()
        print(f"[EXTRACT {index:02d}/{len(pdf_paths):02d}] {pdf_path.name}", flush=True)
        use_case = await build_process_contract(PROJECT_ROOT)
        result = await use_case.execute(pdf_path)
        results.append(result)
        payload = result.model_dump(mode="json")
        payload["experiment_wall_time_seconds"] = round(time.perf_counter() - started, 3)
        await run_blocking(
            _write_yaml_sync,
            extraction_dir / f"{index:02d}_{pdf_path.stem}.yaml",
            payload,
        )
    return results


async def _embed_candidates(
    client: Qwen3VLEmbeddingClient,
    results: Sequence[ContractProcessingResult],
    pdf_by_name: Mapping[str, Path],
) -> tuple[
    dict[str, dict[str, list[float]]],
    dict[str, dict[str, Any]],
]:
    candidate_vectors = {field: {} for field in VECTOR_FIELDS}
    metadata: dict[str, dict[str, Any]] = {}

    async def embed_one(result: ContractProcessingResult) -> None:
        sources = _vector_source_fields(result)
        text_vectors, visual = await asyncio.gather(
            client.embed_text_fields(sources),
            client.embed_pdf(pdf_by_name[result.source_name]),
        )
        visual_vector, page_count = visual
        for field, vector in text_vectors.items():
            candidate_vectors[field][result.source_name] = vector
        candidate_vectors["document_visual_vector"][result.source_name] = visual_vector
        metadata[result.source_name] = {
            "document_id": result.document_id,
            "source_name": result.source_name,
            "vector_source_text": sources,
            "visual_page_count": page_count,
        }

    await asyncio.gather(*(embed_one(result) for result in results))
    return candidate_vectors, metadata


async def _embed_text_query(
    client: Qwen3VLEmbeddingClient, field: str, text: str
) -> list[float]:
    return (await client.embed_text_fields({field: text}))[field]


async def _evaluate_text_fields(
    *,
    client: Qwen3VLEmbeddingClient,
    cases: dict[str, Any],
    candidates: dict[str, dict[str, list[float]]],
    metadata: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, object]]]:
    jobs: list[tuple[str, str, str, str, str]] = []
    for contract_index, (source_name, spec) in enumerate(
        cases["contracts"].items(), start=1
    ):
        hyde = spec["abstract_hyde"]
        jobs.append(
            (
                "abstract_vector",
                f"abstract_hyde_{contract_index:02d}",
                "hyde_hypothetical_document",
                source_name,
                hyde["hypothetical_document"],
            )
        )
        for query in spec["contract_name_queries"]:
            jobs.append(
                ("contract_name_vector", query["id"], query["kind"], source_name, query["text"])
            )
        for query in spec["product_name_queries"]:
            jobs.append(
                ("product_names_vector", query["id"], query["kind"], source_name, query["text"])
            )

    async def run_job(job: tuple[str, str, str, str, str]) -> tuple[str, dict[str, object]]:
        field, query_id, kind, expected, text = job
        vector = await _embed_text_query(client, field, text)
        result = evaluate_query(
            query_id=query_id,
            query_kind=kind,
            query_text=text,
            expected_source_name=expected,
            query_vector=vector,
            candidates=candidates[field],
            candidate_metadata=metadata,
        )
        if field == "abstract_vector":
            result["user_query"] = cases["contracts"][expected]["abstract_hyde"]["query"]
        return field, result

    grouped = {field: [] for field in VECTOR_FIELDS}
    for field, result in await asyncio.gather(*(run_job(job) for job in jobs)):
        grouped[field].append(result)
    return grouped


async def _evaluate_visual_field(
    *,
    client: Qwen3VLEmbeddingClient,
    pdf_paths: Sequence[Path],
    candidates: dict[str, dict[str, list[float]]],
    metadata: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    with tempfile.TemporaryDirectory(prefix="contract-vector-visual-") as directory:
        temporary_root = Path(directory)

        async def transform_and_evaluate(
            index: int, source_pdf: Path
        ) -> dict[str, object]:
            target = temporary_root / f"{index:02d}.pdf"
            transformation = await run_blocking(transform_pdf_sync, source_pdf, target)
            vector, query_page_count = await client.embed_pdf(target)
            result = evaluate_query(
                query_id=f"visual_transform_{index:02d}",
                query_kind=transformation.name,
                query_text=None,
                expected_source_name=source_pdf.name,
                query_vector=vector,
                candidates=candidates["document_visual_vector"],
                candidate_metadata=metadata,
            )
            result["transformation"] = {
                "name": transformation.name,
                "details": transformation.details,
                "source_page_count": transformation.source_page_count,
                "query_page_count": query_page_count,
            }
            return result

        return list(
            await asyncio.gather(
                *(transform_and_evaluate(index, path) for index, path in enumerate(pdf_paths, 1))
            )
        )


async def run_experiment(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    if args.max_documents is not None and args.max_documents < 1:
        raise ValueError("--max-documents 必须大于 0。")
    input_dir = await run_blocking(_resolve(args.input_dir).resolve)
    output_root = await run_blocking(_resolve(args.output_dir).resolve)
    cases_path = await run_blocking(_resolve(args.cases).resolve)
    pdf_paths = await run_blocking(lambda: sorted(input_dir.glob("*.pdf")))
    if args.max_documents is not None:
        pdf_paths = pdf_paths[: args.max_documents]
    if not pdf_paths:
        raise FileNotFoundError(f"输入目录没有 PDF：{input_dir}")

    cases = await run_blocking(_load_cases_sync, cases_path)
    _validate_cases(cases, {path.name for path in pdf_paths})
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_root / run_id
    extraction_dir = run_dir / "extraction"
    await run_blocking(extraction_dir.mkdir, parents=True, exist_ok=False)
    status: dict[str, Any] = {
        "experiment": "contract_vector_retrieval",
        "run_id": run_id,
        "status": "running",
        "started_at": started_at.isoformat(),
        "side_effect_boundary": "仅写实验报告；不连接 Elasticsearch，不持久化向量或派生 PDF",
    }
    if args.reuse_extraction_run is not None:
        status["source_extraction_run"] = str(
            await run_blocking(_resolve(args.reuse_extraction_run).resolve)
        )
    await run_blocking(_write_yaml_sync, run_dir / "status.yaml", status)

    client: Qwen3VLEmbeddingClient | None = None
    timings: dict[str, float] = {}
    try:
        phase = time.perf_counter()
        source_extraction_seconds: float | None = None
        if args.reuse_extraction_run is None:
            results = await _extract_contracts(pdf_paths, extraction_dir)
        else:
            source_run = await run_blocking(_resolve(args.reuse_extraction_run).resolve)
            results, source_extraction_seconds = await run_blocking(
                _load_extraction_results_sync, source_run, pdf_paths
            )
            await _copy_reused_extractions(results, extraction_dir, source_run)
        timings["extraction_seconds"] = round(time.perf_counter() - phase, 3)
        timings["source_extraction_wall_time_seconds"] = (
            source_extraction_seconds
            if source_extraction_seconds is not None
            else timings["extraction_seconds"]
        )

        client, embedding, policy = await _build_embedding_client()
        await client.probe()
        phase = time.perf_counter()
        candidates, metadata = await _embed_candidates(
            client, results, {path.name: path for path in pdf_paths}
        )
        timings["candidate_vectorization_seconds"] = round(time.perf_counter() - phase, 3)

        phase = time.perf_counter()
        grouped = await _evaluate_text_fields(
            client=client,
            cases=cases,
            candidates=candidates,
            metadata=metadata,
        )
        grouped["document_visual_vector"] = await _evaluate_visual_field(
            client=client,
            pdf_paths=pdf_paths,
            candidates=candidates,
            metadata=metadata,
        )
        timings["query_vectorization_and_retrieval_seconds"] = round(
            time.perf_counter() - phase, 3
        )
        timings["total_wall_time_seconds"] = round(time.perf_counter() - started, 3)
        timings["effective_pipeline_seconds"] = round(
            timings["source_extraction_wall_time_seconds"]
            + timings["candidate_vectorization_seconds"]
            + timings["query_vectorization_and_retrieval_seconds"],
            3,
        )
        report = {
            "experiment": "contract_vector_retrieval",
            "run_id": run_id,
            "status": "completed",
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "scope": {
                "candidate_count": len(results),
                "source_names": [result.source_name for result in results],
                "storage": "in_memory_only",
                "similarity": "cosine",
                "unique_expected_target_per_query": True,
            },
            "embedding": {
                "model": embedding.model,
                "dimensions": embedding.dimensions,
                "instruction_version": policy.instruction_version,
                "visual_strategy": policy.visual_strategy,
            },
            "timing": timings,
            "candidate_inputs": metadata,
            "summary": {field: summarize_results(grouped[field]) for field in VECTOR_FIELDS},
            "queries": grouped,
        }
        await run_blocking(_write_yaml_sync, run_dir / "result.yaml", report)
        status.update(
            {
                "status": "completed",
                "completed_at": report["completed_at"],
                "result": "result.yaml",
                "extraction": "extraction/*.yaml",
            }
        )
        await run_blocking(_write_yaml_sync, run_dir / "status.yaml", status)
        return run_dir, report
    except BaseException as error:
        status.update(
            {
                "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                "completed_at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error)[:1200],
            }
        )
        await run_blocking(_write_yaml_sync, run_dir / "status.yaml", status)
        raise
    finally:
        if client is not None:
            await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    run_dir, report = asyncio.run(run_experiment(parse_args(argv)))
    print(f"实验结果：{run_dir / 'result.yaml'}")
    for field, summary in report["summary"].items():
        print(
            f"{field}: Recall@1={summary['recall_at_1']}, "
            f"Recall@3={summary['recall_at_3']}, MRR={summary['mean_reciprocal_rank']}"
        )
    return 0


if __name__ == "__main__":
    # IDE 手动启动时只修改这里；显式命令行参数始终优先。
    IDE_INPUT_DIR = "data/input"
    IDE_CASES = "experiments/contract_vector_retrieval/cases.yaml"
    IDE_OUTPUT_DIR = "experiments/outputs/contract_vector_retrieval"
    IDE_MAX_DOCUMENTS: int | None = None
    IDE_REUSE_EXTRACTION_RUN: str | None = None
    resolved_argv = sys.argv[1:] or build_ide_argv(
        input_dir=IDE_INPUT_DIR,
        cases=IDE_CASES,
        output_dir=IDE_OUTPUT_DIR,
        max_documents=IDE_MAX_DOCUMENTS,
        reuse_extraction_run=IDE_REUSE_EXTRACTION_RUN,
    )
    raise SystemExit(main(resolved_argv))
