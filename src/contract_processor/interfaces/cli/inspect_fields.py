#!/usr/bin/env python3
"""字段目录连通性的 CLI 测试入口。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from contract_processor.bootstrap.container import build_inspect_field_catalog
from contract_processor.async_utils import run_blocking
from contract_processor.interfaces.cli.common import resolve_project_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 Core 与 Attribute 字段目录")
    parser.add_argument("--project-root", type=Path, default=None)
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    project_root = await resolve_project_root(args.project_root)
    use_case = await build_inspect_field_catalog(project_root)
    summary = await use_case.execute()
    await run_blocking(
        sys.stdout.write,
        f"Core 字段：{summary.core_count}；Attribute 字段：{summary.attribute_count}\n",
    )
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
