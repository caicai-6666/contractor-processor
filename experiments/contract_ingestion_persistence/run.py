#!/usr/bin/env python3
"""以最新终审 Mock 验证正式四节点入库子图。"""

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

from contract_processor.application.schemas.contract_ingestion import (  # noqa: E402
    ContractReviewConfirmation,
)
from contract_processor.application.workflows.contract_ingestion import (  # noqa: E402
    ContractIngestionNodeError,
)
from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.bootstrap.container import (  # noqa: E402
    build_ingest_reviewed_contract,
)
from contract_processor.infrastructure.persistence import (  # noqa: E402
    elasticsearch_contract_index,
)
from contract_processor.infrastructure.pdf.document_identity import (  # noqa: E402
    compute_document_id,
)
from contract_processor.settings import load_project_settings  # noqa: E402


DEFAULT_OUTPUT_ROOT = Path("experiments/outputs/contract_ingestion_persistence")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="删除重建实验索引后，以 Mock 验证正式入库子图和 PDF 落盘"
    )
    parser.add_argument("--mock-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--index-name",
        default=None,
        help="只能覆盖为带 experiment 标记且非正式索引的测试索引名。",
    )
    parser.add_argument("--limit", type=int, default=None)
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


def _load_mock_manifest_sync(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Mock manifest 不存在：{manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("experiment") != "contract_ingestion_mock":
        raise ValueError("--mock-run 不是 contract_ingestion_mock 运行目录。")
    if "completed_at" not in payload:
        raise RuntimeError("Mock 运行尚未完成。")
    return payload


def validate_recreate_target(
    *, index_name: str, production_index_name: str
) -> None:
    """真实实验必须重建索引，同时把破坏性范围锁死在实验命名空间。"""

    if index_name == production_index_name:
        raise RuntimeError("实验拒绝删除正式合同索引。")
    if "experiment" not in index_name.casefold():
        raise RuntimeError("待重建索引名必须包含 experiment 安全标记。")


async def recreate_experiment_index(
    *,
    client: Any,
    index_name: str,
    production_index_name: str,
    dimensions: int,
    number_of_shards: int = 1,
    number_of_replicas: int = 0,
) -> tuple[bool, str]:
    """每次真实入库测试前删除旧实验索引，并立即按正式 mapping 重建。"""

    validate_recreate_target(
        index_name=index_name,
        production_index_name=production_index_name,
    )
    existed = await client.indices.exists(index=index_name)
    if existed:
        await client.indices.delete(index=index_name)
    store = elasticsearch_contract_index.Elasticsearch9ContractVectorStore(
        client=client,
        index_name=index_name,
        dimensions=dimensions,
        number_of_shards=number_of_shards,
        number_of_replicas=number_of_replicas,
    )
    return bool(existed), await store.ensure_index()


def _load_confirmation_sync(path: Path) -> ContractReviewConfirmation:
    return ContractReviewConfirmation.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def _contains_empty(value: Any) -> bool:
    """验证稀疏检索投影中没有 null、空字符串或空容器。"""

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return not value or any(_contains_empty(item) for item in value.values())
    if isinstance(value, list):
        return not value or any(_contains_empty(item) for item in value)
    return False


async def verify_persisted_contracts(
    *,
    client: AsyncElasticsearch,
    index_name: str,
    records: list[dict[str, Any]],
    dimensions: int,
    source_document_root: Path,
) -> dict[str, Any]:
    """强制读取 ES 向量和 PDF，形成可审计的端到端验证摘要。"""

    succeeded = [record for record in records if record.get("status") == "succeeded"]
    expected_ids = {str(record["document_id"]) for record in succeeded}
    count = int((await client.count(index=index_name))["count"])
    if count != len(expected_ids):
        raise RuntimeError(
            f"ES 文档数为 {count}，成功入库 document_id 数为 {len(expected_ids)}。"
        )
    verified: list[dict[str, Any]] = []
    for document_id in sorted(expected_ids):
        response = await client.get(
            index=index_name,
            id=document_id,
            source_exclude_vectors=False,
        )
        source = response.get("_source")
        if not isinstance(source, dict):
            raise RuntimeError(f"ES 文档 {document_id} 缺少 _source。")
        vector_fields = sorted(
            field
            for field in elasticsearch_contract_index.VECTOR_FIELDS
            if source.get(field) is not None
        )
        for field in vector_fields:
            vector = source[field]
            if not isinstance(vector, list) or len(vector) != dimensions:
                raise RuntimeError(f"{document_id}.{field} 向量维度异常。")
        if "document_visual_vector" not in vector_fields:
            raise RuntimeError(f"{document_id} 缺少必填视觉向量。")
        if _contains_empty(source.get("core_values")):
            raise RuntimeError(f"{document_id}.core_values 仍包含空值。")
        attribute_values = source.get("attribute_values")
        if attribute_values and _contains_empty(attribute_values):
            raise RuntimeError(f"{document_id}.attribute_values 仍包含空值。")
        source_document = source.get("source_document")
        if not isinstance(source_document, dict):
            raise RuntimeError(f"{document_id} 缺少 source_document。")
        storage_key = source_document.get("storage_key")
        if storage_key != f"{document_id}.pdf":
            raise RuntimeError(f"{document_id} 的 PDF 存储键不符合身份协议。")
        stored_pdf = source_document_root / storage_key
        if await compute_document_id(stored_pdf) != document_id:
            raise RuntimeError(f"{document_id} 的落盘 PDF 哈希校验失败。")
        verified.append(
            {
                "document_id": document_id,
                "vector_fields": vector_fields,
                "vector_dimensions": dimensions,
                "storage_key": storage_key,
                "pdf_hash_verified": True,
                "sparse_projection_verified": True,
            }
        )
    return {
        "document_count": count,
        "unique_document_ids": len(expected_ids),
        "all_vectors_verified": True,
        "all_pdf_hashes_verified": True,
        "all_sparse_projections_verified": True,
        "contracts": verified,
    }


async def async_main() -> tuple[Path, int]:
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit 必须大于 0。")
    mock_run = _resolve(args.mock_run)
    output_root = _resolve(args.output_dir)
    mock_manifest = await run_blocking(_load_mock_manifest_sync, mock_run)
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    settings = await load_project_settings(PROJECT_ROOT)
    elasticsearch = settings.elasticsearch
    embedding = settings.models.embedding
    index_name = args.index_name or elasticsearch.ingestion_experiment_index_name
    validate_recreate_target(
        index_name=index_name,
        production_index_name=elasticsearch.index_name,
    )

    username = os.getenv(elasticsearch.username_env)
    password = os.getenv(elasticsearch.password_env)
    if not username or not password:
        raise RuntimeError("Elasticsearch 用户名或密码环境变量未配置。")
    ca_certs = elasticsearch.ca_certs
    if ca_certs is not None and not ca_certs.is_absolute():
        ca_certs = PROJECT_ROOT / ca_certs
    maintenance_client = AsyncElasticsearch(
        elasticsearch.hosts,
        basic_auth=(username, password),
        ca_certs=str(ca_certs) if ca_certs is not None else None,
        verify_certs=elasticsearch.verify_certs,
        request_timeout=60,
    )
    try:
        deleted_existing, index_status = await recreate_experiment_index(
            client=maintenance_client,
            index_name=index_name,
            production_index_name=elasticsearch.index_name,
            dimensions=embedding.dimensions,
            number_of_shards=elasticsearch.number_of_shards,
            number_of_replicas=elasticsearch.number_of_replicas,
        )
    finally:
        await maintenance_client.close()

    use_case = await build_ingest_reviewed_contract(
        PROJECT_ROOT,
        index_name=index_name,
    )
    started_at = datetime.now(UTC)
    run_dir = output_root / started_at.strftime("%Y%m%dT%H%M%S%fZ")
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "experiment": "contract_ingestion_persistence",
        "implementation": "formal_contract_ingestion_subgraph",
        "started_at": started_at.isoformat(),
        "source_mock_run": _project_path(mock_run),
        "index_name": index_name,
        "index_deleted_before_test": deleted_existing,
        "index_recreated_before_test": index_status == "created",
        "embedding_model": embedding.model,
        "vector_dimensions": embedding.dimensions,
        "source_document_root": _project_path(
            _resolve(settings.paths.source_documents)
        ),
        "contracts": [],
        "upstream_failures": [
            item
            for item in mock_manifest.get("contracts", [])
            if item.get("status") != "succeeded"
        ],
    }
    manifest_path = run_dir / "manifest.json"
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
    failure_count = 0
    try:
        manifest["index_validation_status"] = await use_case.initialize()
        succeeded = [
            item
            for item in mock_manifest.get("contracts", [])
            if item.get("status") == "succeeded"
        ]
        if args.limit is not None:
            succeeded = succeeded[: args.limit]
        for item in succeeded:
            package_path = (mock_run / item["package"]).resolve()
            source_pdf = _resolve(Path(item["source_pdf"]))
            record: dict[str, Any] = {
                "index": item["index"],
                "document_id": item["document_id"],
                "source_name": item["source_name"],
                "source_pdf": _project_path(source_pdf),
                "package": _project_path(package_path),
            }
            try:
                confirmation = await run_blocking(
                    _load_confirmation_sync, package_path
                )
                outcome = await use_case.execute(confirmation, source_pdf)
                result_name = f"{item['index']:02d}_ingestion_result.json"
                await run_blocking(
                    _write_json_atomic_sync,
                    run_dir / result_name,
                    outcome.as_dict(),
                )
                record.update({"status": "succeeded", "result": result_name})
            except ContractIngestionNodeError as error:
                failure_count += 1
                diagnostic_name = f"{item['index']:02d}_failure_diagnostic.json"
                diagnostic = {
                    "node": error.node,
                    "error_type": error.cause_type,
                    "error": str(error),
                }
                await run_blocking(
                    _write_json_atomic_sync,
                    run_dir / diagnostic_name,
                    diagnostic,
                )
                record.update(
                    {
                        "status": "failed",
                        **diagnostic,
                        "diagnostic": diagnostic_name,
                    }
                )
            manifest["contracts"].append(record)
            await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
    except Exception as error:
        failure_count += 1
        diagnostic_name = "runtime_failure_diagnostic.json"
        diagnostic = {"error_type": type(error).__name__, "error": str(error)}
        await run_blocking(
            _write_json_atomic_sync,
            run_dir / diagnostic_name,
            diagnostic,
        )
        manifest["runtime_failure"] = {**diagnostic, "diagnostic": diagnostic_name}
    finally:
        await use_case.close()

    verification_client = AsyncElasticsearch(
        elasticsearch.hosts,
        basic_auth=(username, password),
        ca_certs=str(ca_certs) if ca_certs is not None else None,
        verify_certs=elasticsearch.verify_certs,
        request_timeout=60,
    )
    try:
        manifest["verification"] = await verify_persisted_contracts(
            client=verification_client,
            index_name=index_name,
            records=manifest["contracts"],
            dimensions=embedding.dimensions,
            source_document_root=_resolve(settings.paths.source_documents),
        )
    except Exception as error:
        failure_count += 1
        manifest["verification_failure"] = {
            "error_type": type(error).__name__,
            "error": str(error),
        }
    finally:
        await verification_client.close()

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["succeeded_count"] = sum(
        item.get("status") == "succeeded" for item in manifest["contracts"]
    )
    manifest["failure_count"] = failure_count
    manifest["upstream_failure_count"] = len(manifest["upstream_failures"])
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
    return run_dir, failure_count


def main() -> int:
    run_dir, failure_count = asyncio.run(async_main())
    print(run_dir)
    return 1 if failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
