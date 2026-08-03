#!/usr/bin/env python3
"""生成删页/旋转缩放 PDF，并以视觉向量查询原合同索引。"""

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
for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dotenv import load_dotenv  # noqa: E402
from elasticsearch import AsyncElasticsearch  # noqa: E402
from llama_index.core.vector_stores.types import VectorStoreQuery  # noqa: E402

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.settings import load_project_settings  # noqa: E402
from experiments.contract_ingestion_persistence.embedding import Qwen3VLEmbeddingClient, load_contract_embedding_policy  # noqa: E402
from experiments.contract_ingestion_persistence.vector_store import Elasticsearch9ContractVectorStore  # noqa: E402
from experiments.contract_visual_retrieval.evaluation import assess_visual_retrieval, summarize_visual_retrieval  # noqa: E402
from experiments.contract_visual_robustness.transformations import transform_pdf_sync  # noqa: E402


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_contracts(run: Path) -> list[dict[str, Any]]:
    payload = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    if payload.get("experiment") != "contract_ingestion_mock" or not payload.get("completed_at"):
        raise ValueError("--mock-run 必须是已完成的 contract_ingestion_mock 运行。")
    return [item for item in payload["contracts"] if item.get("status") == "succeeded"]


def report(rows: list[tuple[dict[str, Any], Any]]) -> str:
    lines = ["# 变换合同视觉召回报告", "", "仅使用 `document_visual_vector` 的 ES KNN 得分。", ""]
    for record, result in rows:
        lines += [f"## {record['source_name']}", "", f"变换：{record['transformation']['details']}；原合同排名：**{result.rank}**。", "", "| 排名 | 候选合同 | ES KNN 得分 | 原合同 |", "| ---: | --- | ---: | :---: |"]
        lines += [f"| {c.rank} | {c.source_name or c.document_id} | {c.score:.6f} | {'是' if c.is_expected else ''} |" for c in result.candidates]
        lines.append("")
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description="评估变换后合同的视觉召回")
    parser.add_argument("--mock-run", type=Path, required=True)
    args = parser.parse_args()
    mock_run = args.mock_run if args.mock_run.is_absolute() else PROJECT_ROOT / args.mock_run
    contracts = await run_blocking(read_contracts, mock_run)
    expected_ids = {str(item["document_id"]) for item in contracts}
    settings = await load_project_settings(PROJECT_ROOT)
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    username, password = os.getenv(settings.elasticsearch.username_env), os.getenv(settings.elasticsearch.password_env)
    if not username or not password:
        raise RuntimeError("Elasticsearch 凭据未配置。")
    ca = settings.elasticsearch.ca_certs
    if ca and not ca.is_absolute(): ca = PROJECT_ROOT / ca
    es = AsyncElasticsearch(settings.elasticsearch.hosts, basic_auth=(username, password), ca_certs=str(ca) if ca else None, verify_certs=settings.elasticsearch.verify_certs, request_timeout=60)
    embed = settings.models.embedding
    policy_path = settings.paths.contract_embedding_policy
    if not policy_path.is_absolute(): policy_path = PROJECT_ROOT / policy_path
    policy = await load_contract_embedding_policy(policy_path)
    client = Qwen3VLEmbeddingClient(base_url=embed.base_url, api_key=os.getenv(embed.api_key_env) or "", model=embed.model, endpoint=embed.endpoint, timeout_seconds=embed.timeout_seconds, dimensions=embed.dimensions, max_concurrent_requests=embed.max_concurrent_requests, normalize=embed.normalize, policy=policy)
    store = Elasticsearch9ContractVectorStore(client=es, index_name=settings.elasticsearch.ingestion_experiment_index_name, dimensions=embed.dimensions)
    run_dir = PROJECT_ROOT / "experiments/outputs/contract_visual_robustness" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    transformed = run_dir / "transformed"
    await run_blocking(transformed.mkdir, parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"experiment": "contract_visual_robustness", "started_at": datetime.now(UTC).isoformat(), "source_mock_run": str(mock_run.relative_to(PROJECT_ROOT)), "contracts": []}
    try:
        await client.probe(); await store.ensure_index()
        hits = await es.search(index=store.index_name, query={"match_all": {}}, size=1000, source=False, track_total_hits=True)
        ids = {str(hit["_id"]) for hit in hits["hits"]["hits"]}
        if ids != expected_ids: raise RuntimeError("索引候选集与 5 份 Mock 成功合同不一致。")
        rows = []
        for item in contracts:
            source = PROJECT_ROOT / item["source_pdf"]
            target = transformed / f"{int(item['index']):02d}_transformed.pdf"
            change = await run_blocking(transform_pdf_sync, source, target)
            vector, pages = await client.embed_pdf(target)
            query = await store.aquery(VectorStoreQuery(query_embedding=vector, embedding_field="document_visual_vector", similarity_top_k=len(ids)))
            names = [node.metadata.get("source_name") if isinstance(node.metadata.get("source_name"), str) else None for node in query.nodes]
            result = assess_visual_retrieval(expected_document_id=str(item["document_id"]), expected_source_name=str(item["source_name"]), visual_page_count=pages, retrieved_ids=query.ids, scores=query.similarities, source_names=names)
            result_file = f"{int(item['index']):02d}_retrieval.json"
            await run_blocking(write_json, run_dir / result_file, result.as_dict())
            record = {"index": item["index"], "source_name": item["source_name"], "document_id": item["document_id"], "transformed_pdf": str(target.relative_to(run_dir)), "transformation": change.__dict__ if hasattr(change, "__dict__") else {"name": change.name, "details": change.details, "source_page_count": change.source_page_count, "transformed_page_count": change.transformed_page_count}, "rank": result.rank, "result": result_file}
            manifest["contracts"].append(record); rows.append((record, result))
        manifest["metrics"] = summarize_visual_retrieval([result for _, result in rows], candidate_count=len(ids))
        await run_blocking(lambda: (run_dir / "report.md").write_text(report(rows), encoding="utf-8"))
        manifest["report"] = "report.md"; code = 0
    except Exception as error:
        manifest["runtime_failure"] = {"error_type": type(error).__name__, "error": str(error)}; code = 1
    finally:
        await client.close(); await es.close()
    manifest["completed_at"] = datetime.now(UTC).isoformat(); manifest["failure_count"] = code
    await run_blocking(write_json, run_dir / "manifest.json", manifest)
    print(run_dir)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
