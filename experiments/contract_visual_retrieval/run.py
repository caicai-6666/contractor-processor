#!/usr/bin/env python3
"""以重算 PDF 视觉向量评估当前入库合同的自查询召回排名。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from dotenv import load_dotenv  # noqa: E402
from elasticsearch import AsyncElasticsearch  # noqa: E402
from llama_index.core.vector_stores.types import VectorStoreQuery  # noqa: E402

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.settings import load_project_settings  # noqa: E402
from experiments.contract_ingestion_persistence.embedding import (  # noqa: E402
    Qwen3VLEmbeddingClient,
    load_contract_embedding_policy,
)
from experiments.contract_ingestion_persistence.vector_store import (  # noqa: E402
    Elasticsearch9ContractVectorStore,
)
from experiments.contract_visual_retrieval.evaluation import (  # noqa: E402
    VisualRetrievalResult,
    assess_visual_retrieval,
    summarize_visual_retrieval,
)


DEFAULT_OUTPUT_ROOT = Path("experiments/outputs/contract_visual_retrieval")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重算 PDF 视觉向量，并评估它在当前入库候选集中的自查询排名"
    )
    parser.add_argument(
        "--mock-run",
        type=Path,
        required=True,
        help="明确指定包含待评估 PDF 清单的已完成 contract_ingestion_mock 运行目录。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="实验结果目录；不保存高维向量或渲染页面。",
    )
    parser.add_argument(
        "--index-name",
        default=None,
        help="覆盖配置中的入库实验索引名。",
    )
    return parser.parse_args()


def _resolve(path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _project_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _write_json_atomic_sync(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_mock_manifest_sync(run_dir: Path) -> list[dict[str, str | int]]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Mock manifest 不存在：{manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("experiment") != "contract_ingestion_mock":
        raise ValueError("--mock-run 不是 contract_ingestion_mock 运行目录。")
    if not payload.get("completed_at"):
        raise RuntimeError("Mock 运行尚未完成，不允许读取仍在写入的清单。")
    contracts = [
        item for item in payload.get("contracts", []) if item.get("status") == "succeeded"
    ]
    if not contracts:
        raise RuntimeError("指定 Mock 运行没有成功合同，无法进行视觉召回评估。")
    required = ("index", "document_id", "source_name", "source_pdf")
    for item in contracts:
        if any(not item.get(key) for key in required):
            raise ValueError(f"Mock 成功项缺少评估所需字段：{item}")
    return contracts


async def _load_index_document_ids(
    client: AsyncElasticsearch, *, index_name: str
) -> set[str]:
    """读取候选集 ID；实验要求其与指定 Mock 的成功合同严格一致。"""

    response = await client.search(
        index=index_name,
        query={"match_all": {}},
        size=1000,
        source=False,
        track_total_hits=True,
    )
    total = response["hits"]["total"]
    count = int(total["value"] if isinstance(total, dict) else total)
    if count > 1000:
        raise RuntimeError("候选索引超过 1000 条，当前实验拒绝截断候选集。")
    identifiers = {str(hit["_id"]) for hit in response["hits"]["hits"]}
    if len(identifiers) != count:
        raise RuntimeError("索引返回的 document_id 数量与总数不一致。")
    return identifiers


async def _evaluate_contract(
    *,
    item: dict[str, str | int],
    embedding_client: Qwen3VLEmbeddingClient,
    vector_store: Elasticsearch9ContractVectorStore,
    candidate_count: int,
) -> VisualRetrievalResult:
    source_pdf = _resolve(Path(str(item["source_pdf"])))
    if not source_pdf.is_file():
        raise FileNotFoundError(f"源 PDF 不存在：{source_pdf}")
    vector, page_count = await embedding_client.embed_pdf(source_pdf)
    query_result = await vector_store.aquery(
        VectorStoreQuery(
            query_embedding=vector,
            embedding_field="document_visual_vector",
            similarity_top_k=candidate_count,
        )
    )
    source_names = [
        node.metadata.get("source_name")
        if isinstance(node.metadata.get("source_name"), str)
        else None
        for node in query_result.nodes
    ]
    return assess_visual_retrieval(
        expected_document_id=str(item["document_id"]),
        expected_source_name=str(item["source_name"]),
        visual_page_count=page_count,
        retrieved_ids=query_result.ids,
        scores=query_result.similarities,
        source_names=source_names,
    )


async def async_main() -> tuple[Path, int]:
    args = parse_args()
    mock_run = _resolve(args.mock_run)
    output_root = _resolve(args.output_dir)
    contracts = await run_blocking(_load_mock_manifest_sync, mock_run)
    expected_ids = {str(item["document_id"]) for item in contracts}
    if len(expected_ids) != len(contracts):
        raise RuntimeError("Mock 成功合同存在重复 document_id，无法定义候选排名。")

    settings = await load_project_settings(PROJECT_ROOT)
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    embedding = settings.models.embedding
    embedding_policy_path = settings.paths.contract_embedding_policy
    if not embedding_policy_path.is_absolute():
        embedding_policy_path = PROJECT_ROOT / embedding_policy_path
    embedding_policy = await load_contract_embedding_policy(embedding_policy_path)
    dimensions = embedding.dimensions
    if settings.elasticsearch.vector_dimensions != dimensions:
        raise RuntimeError(
            "elasticsearch.vector_dimensions 与 models.embedding.dimensions 不一致。"
        )
    username = os.getenv(settings.elasticsearch.username_env)
    password = os.getenv(settings.elasticsearch.password_env)
    if not username or not password:
        raise RuntimeError("Elasticsearch 用户名或密码环境变量未配置。")
    ca_certs = settings.elasticsearch.ca_certs
    if ca_certs is not None and not ca_certs.is_absolute():
        ca_certs = PROJECT_ROOT / ca_certs
    index_name = args.index_name or settings.elasticsearch.ingestion_experiment_index_name
    es_client = AsyncElasticsearch(
        settings.elasticsearch.hosts,
        basic_auth=(username, password),
        ca_certs=str(ca_certs) if ca_certs is not None else None,
        verify_certs=settings.elasticsearch.verify_certs,
        request_timeout=60,
    )
    embedding_client = Qwen3VLEmbeddingClient(
        base_url=embedding.base_url,
        api_key=os.getenv(embedding.api_key_env) or "",
        model=embedding.model,
        endpoint=embedding.endpoint,
        timeout_seconds=embedding.timeout_seconds,
        dimensions=dimensions,
        max_concurrent_requests=embedding.max_concurrent_requests,
        normalize=embedding.normalize,
        policy=embedding_policy,
    )
    vector_store = Elasticsearch9ContractVectorStore(
        client=es_client,
        index_name=index_name,
        dimensions=dimensions,
    )
    started_at = datetime.now(UTC)
    run_dir = output_root / started_at.strftime("%Y%m%dT%H%M%S%fZ")
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "experiment": "contract_visual_retrieval",
        "started_at": started_at.isoformat(),
        "source_mock_run": _project_path(mock_run),
        "index_name": index_name,
        "embedding_model": embedding.model,
        "vector_dimensions": dimensions,
        "retrieval_vector_field": "document_visual_vector",
        "contracts": [],
    }
    manifest_path = run_dir / "manifest.json"
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
    results: list[VisualRetrievalResult] = []
    failure_count = 0
    try:
        await embedding_client.probe()
        manifest["index_status"] = await vector_store.ensure_index()
        actual_ids = await _load_index_document_ids(es_client, index_name=index_name)
        if actual_ids != expected_ids:
            missing = sorted(expected_ids - actual_ids)
            unexpected = sorted(actual_ids - expected_ids)
            raise RuntimeError(
                "索引候选集与指定 Mock 成功合同不一致："
                f"missing={missing}, unexpected={unexpected}。"
                "请先使用同一 Mock 运行完成入库，或指定对应索引。"
            )
        manifest["candidate_count"] = len(actual_ids)
        await run_blocking(_write_json_atomic_sync, manifest_path, manifest)

        async def evaluate_one(
            item: dict[str, str | int],
        ) -> tuple[dict[str, Any], VisualRetrievalResult | None]:
            record: dict[str, Any] = {
                "index": item["index"],
                "document_id": item["document_id"],
                "source_name": item["source_name"],
                "source_pdf": item["source_pdf"],
            }
            try:
                result = await _evaluate_contract(
                    item=item,
                    embedding_client=embedding_client,
                    vector_store=vector_store,
                    candidate_count=len(actual_ids),
                )
                result_name = f"{int(item['index']):02d}_visual_retrieval.json"
                await run_blocking(
                    _write_json_atomic_sync, run_dir / result_name, result.as_dict()
                )
                record.update({"status": "succeeded", "result": result_name, "rank": result.rank})
                return record, result
            except Exception as error:
                diagnostic_name = f"{int(item['index']):02d}_failure_diagnostic.json"
                await run_blocking(
                    _write_json_atomic_sync,
                    run_dir / diagnostic_name,
                    {"error_type": type(error).__name__, "error": str(error)},
                )
                record.update(
                    {
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "diagnostic": diagnostic_name,
                    }
                )
                return record, None

        evaluated = await asyncio.gather(*(evaluate_one(item) for item in contracts))
        for record, result in sorted(evaluated, key=lambda pair: int(pair[0]["index"])):
            manifest["contracts"].append(record)
            if result is None:
                failure_count += 1
            else:
                results.append(result)
        manifest["metrics"] = summarize_visual_retrieval(
            results, candidate_count=len(actual_ids)
        )
    except Exception as error:
        failure_count += 1
        diagnostic_name = "runtime_failure_diagnostic.json"
        diagnostic = {"error_type": type(error).__name__, "error": str(error)}
        await run_blocking(_write_json_atomic_sync, run_dir / diagnostic_name, diagnostic)
        manifest["runtime_failure"] = {**diagnostic, "diagnostic": diagnostic_name}
    finally:
        await embedding_client.close()
        await es_client.close()

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["succeeded_count"] = len(results)
    manifest["failure_count"] = failure_count
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
    return run_dir, failure_count


def main() -> int:
    run_dir, failure_count = asyncio.run(async_main())
    print(run_dir)
    return 1 if failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
