#!/usr/bin/env python3
"""安全清空入库实验索引中的文档，同时保留 mapping 与索引本身。"""

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
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv  # noqa: E402
from elasticsearch import AsyncElasticsearch  # noqa: E402

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.settings import load_project_settings  # noqa: E402


EXPERIMENT_INDEX_MARKER = "experiment"


@dataclass(frozen=True, slots=True)
class ClearIndexOutcome:
    """预览和实际执行共用的机器可读结果。"""

    index_name: str
    executed: bool
    documents_before: int
    deleted: int
    documents_after: int
    mapping_preserved: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清空入库实验索引文档；默认只预览，不删除索引或 mapping"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际执行 delete_by_query；省略时只显示当前文档数。",
    )
    parser.add_argument(
        "--confirm-index-name",
        default=None,
        help="执行时必须再次完整输入配置中的实验索引名。",
    )
    return parser.parse_args()


def validate_clear_target(
    *,
    index_name: str,
    production_index_name: str,
    execute: bool,
    confirmed_index_name: str | None,
) -> None:
    """多重门禁阻止配置错误把正式合同索引当成实验索引清空。"""

    if index_name == production_index_name:
        raise RuntimeError("入库实验索引与正式合同索引相同，拒绝清空。")
    if EXPERIMENT_INDEX_MARKER not in index_name.casefold():
        raise RuntimeError(
            f"索引名 {index_name!r} 不包含安全标记 {EXPERIMENT_INDEX_MARKER!r}，拒绝清空。"
        )
    if not execute:
        return
    if confirmed_index_name != index_name:
        raise RuntimeError(
            "执行清空时 --confirm-index-name 必须与配置中的实验索引名完全一致。"
        )


async def clear_index_documents(
    *,
    client: Any,
    index_name: str,
    production_index_name: str,
    execute: bool,
    confirmed_index_name: str | None,
) -> ClearIndexOutcome:
    """删除全部文档但保留索引；删除后强制计数验证，避免报告虚假成功。"""

    validate_clear_target(
        index_name=index_name,
        production_index_name=production_index_name,
        execute=execute,
        confirmed_index_name=confirmed_index_name,
    )
    if not await client.indices.exists(index=index_name):
        raise RuntimeError(f"实验索引不存在：{index_name}")
    documents_before = int((await client.count(index=index_name))["count"])
    if not execute:
        return ClearIndexOutcome(
            index_name=index_name,
            executed=False,
            documents_before=documents_before,
            deleted=0,
            documents_after=documents_before,
        )

    response = await client.delete_by_query(
        index=index_name,
        query={"match_all": {}},
        conflicts="proceed",
        refresh=True,
        wait_for_completion=True,
    )
    failures = response.get("failures") or []
    version_conflicts = int(response.get("version_conflicts") or 0)
    if failures or version_conflicts:
        raise RuntimeError(
            "实验索引清空未完整成功："
            f"version_conflicts={version_conflicts}, failures={failures}"
        )
    deleted = int(response.get("deleted") or 0)
    documents_after = int((await client.count(index=index_name))["count"])
    if documents_after != 0:
        raise RuntimeError(
            f"删除请求完成后索引仍有 {documents_after} 条文档；"
            "可能存在并发写入，拒绝报告清空成功。"
        )
    return ClearIndexOutcome(
        index_name=index_name,
        executed=True,
        documents_before=documents_before,
        deleted=deleted,
        documents_after=documents_after,
    )


async def async_main() -> ClearIndexOutcome:
    args = parse_args()
    settings = await load_project_settings(PROJECT_ROOT)
    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    elasticsearch = settings.elasticsearch
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
        return await clear_index_documents(
            client=client,
            index_name=elasticsearch.ingestion_experiment_index_name,
            production_index_name=elasticsearch.index_name,
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
