#!/usr/bin/env python3
"""安全重建零文档入库实验索引，使创建期 mapping 变更生效。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
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

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.settings import load_project_settings  # noqa: E402
from experiments.contract_ingestion_persistence.clear_index import (  # noqa: E402
    validate_clear_target,
)
from experiments.contract_ingestion_persistence.vector_store import (  # noqa: E402
    CHINESE_TEXT_ANALYZER,
    Elasticsearch9ContractVectorStore,
    find_mapping_incompatibilities,
)


@dataclass(frozen=True, slots=True)
class RebuildIndexOutcome:
    """记录预览或实际重建结果。"""

    index_name: str
    executed: bool
    documents_before: int
    mapping_compatible_before: bool
    mapping_status_after: str | None
    chinese_analyzer: str = CHINESE_TEXT_ANALYZER


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="仅重建零文档入库实验索引；默认只预览"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际删除并重建零文档索引；省略时只检查。",
    )
    parser.add_argument(
        "--confirm-index-name",
        default=None,
        help="执行时必须再次完整输入配置中的实验索引名。",
    )
    return parser.parse_args()


async def rebuild_empty_experiment_index(
    *,
    client: Any,
    index_name: str,
    production_index_name: str,
    dimensions: int,
    execute: bool,
    confirmed_index_name: str | None,
) -> RebuildIndexOutcome:
    """在写阻断保护下，仅删除并重建已经为空的实验索引。"""

    validate_clear_target(
        index_name=index_name,
        production_index_name=production_index_name,
        execute=execute,
        confirmed_index_name=confirmed_index_name,
    )
    exists_before = await client.indices.exists(index=index_name)
    if exists_before:
        documents_before = int((await client.count(index=index_name))["count"])
        response = await client.indices.get_mapping(index=index_name)
        mappings = dict(response)[index_name].get("mappings", {})
        compatible_before = not find_mapping_incompatibilities(mappings, dimensions)
    else:
        # 支持“旧空索引已删、按新 mapping 创建失败”后的显式恢复。
        documents_before = 0
        compatible_before = False
    if not execute:
        return RebuildIndexOutcome(
            index_name=index_name,
            executed=False,
            documents_before=documents_before,
            mapping_compatible_before=compatible_before,
            mapping_status_after=None,
        )
    if not exists_before:
        store = Elasticsearch9ContractVectorStore(
            client=client,
            index_name=index_name,
            dimensions=dimensions,
        )
        status = await store.ensure_index()
        return RebuildIndexOutcome(
            index_name=index_name,
            executed=True,
            documents_before=documents_before,
            mapping_compatible_before=compatible_before,
            mapping_status_after=status,
        )
    if documents_before != 0:
        raise RuntimeError(
            f"实验索引仍有 {documents_before} 条文档，拒绝重建；请先安全清空。"
        )

    # 先封锁并发写入，再二次计数，避免检查为空后又写入文档的竞态窗口。
    await client.indices.put_settings(
        index=index_name,
        settings={"index.blocks.write": True},
    )
    deleted = False
    try:
        locked_count = int((await client.count(index=index_name))["count"])
        if locked_count != 0:
            raise RuntimeError(
                f"写入封锁后索引出现 {locked_count} 条文档，拒绝重建。"
            )
        await client.indices.delete(index=index_name)
        deleted = True
        store = Elasticsearch9ContractVectorStore(
            client=client,
            index_name=index_name,
            dimensions=dimensions,
        )
        status = await store.ensure_index()
    except Exception:
        # 删除前出错时恢复可写；若已经删除，则不能把旧 mapping 无声重建回来。
        if not deleted and await client.indices.exists(index=index_name):
            await client.indices.put_settings(
                index=index_name,
                settings={"index.blocks.write": False},
            )
        raise

    return RebuildIndexOutcome(
        index_name=index_name,
        executed=True,
        documents_before=documents_before,
        mapping_compatible_before=compatible_before,
        mapping_status_after=status,
    )


async def async_main() -> RebuildIndexOutcome:
    args = parse_args()
    settings = await load_project_settings(PROJECT_ROOT)
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    elasticsearch = settings.elasticsearch
    dimensions = settings.models.embedding.dimensions
    if elasticsearch.vector_dimensions != dimensions:
        raise RuntimeError(
            "elasticsearch.vector_dimensions 与 models.embedding.dimensions 不一致。"
        )
    username = os.getenv(elasticsearch.username_env)
    password = os.getenv(elasticsearch.password_env)
    if not username or not password:
        raise RuntimeError("Elasticsearch 用户名或密码环境变量未配置。")
    ca_certs = elasticsearch.ca_certs
    if ca_certs is not None and not ca_certs.is_absolute():
        ca_certs = PROJECT_ROOT / ca_certs
    client = AsyncElasticsearch(
        elasticsearch.hosts,
        basic_auth=(username, password),
        ca_certs=str(ca_certs) if ca_certs is not None else None,
        verify_certs=elasticsearch.verify_certs,
        request_timeout=60,
    )
    try:
        return await rebuild_empty_experiment_index(
            client=client,
            index_name=elasticsearch.ingestion_experiment_index_name,
            production_index_name=elasticsearch.index_name,
            dimensions=dimensions,
            execute=args.execute,
            confirmed_index_name=args.confirm_index_name,
        )
    finally:
        await client.close()


def main() -> int:
    outcome = asyncio.run(async_main())
    print(json.dumps(asdict(outcome), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
