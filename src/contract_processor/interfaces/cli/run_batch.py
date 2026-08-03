#!/usr/bin/env python3
"""批量合同工作流的无落盘 CLI 入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from contract_processor.application.schemas.contract_processing import (
    ContractProcessingResult,
)
from contract_processor.bootstrap.container import build_contract_runtime
from contract_processor.async_utils import run_blocking
from contract_processor.domain.enums import RuntimeMode
from contract_processor.interfaces.cli.common import (
    load_cli_settings,
    resolve_from_root,
    resolve_project_root,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按显式模式批量异步运行合同工作流")
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument(
        "--mode",
        type=RuntimeMode,
        choices=[mode.value for mode in RuntimeMode],
        default=None,
        help="运行模式；未指定时读取 settings.runtime.mode。",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="PDF 目录；默认使用 settings.paths.input_contracts。",
    )
    return parser.parse_args()


async def async_main() -> int:
    """逐份等待异步工作流，避免单个本地 vLLM 实例被无界并发压垮。"""

    args = parse_args()
    project_root = await resolve_project_root(args.project_root)
    settings = await load_cli_settings(project_root)
    input_dir = await resolve_from_root(
        args.input_dir or settings.paths.input_contracts,
        project_root,
    )
    pdf_paths = (
        await run_blocking(lambda: sorted(input_dir.glob("*.pdf")))
        if await run_blocking(input_dir.is_dir)
        else []
    )
    if not pdf_paths:
        raise RuntimeError(f"合同目录中没有 PDF：{input_dir}")

    use_case = await build_contract_runtime(project_root, mode=args.mode)
    summaries: list[dict[str, Any]] = []
    for pdf_path in pdf_paths:
        result = await use_case.execute(pdf_path)
        is_production = isinstance(result, ContractProcessingResult)
        summary: dict[str, Any] = {
            "mode": "production" if is_production else "discovery",
            "source_name": result.source_name,
            "document_id": result.document_id,
            "status": "succeeded",
            "core_field_count": len(result.core),
        }
        if is_production:
            summary.update(
                {
                    "attribute_count": len(result.attribute),
                    "clause_count": len(result.clause),
                    "abstract_characters": len(result.abstract.text.strip()),
                }
            )
        else:
            summary["candidate_count"] = len(result.candidates)
        summaries.append(summary)

    await run_blocking(
        sys.stdout.write,
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n",
    )
    await run_blocking(sys.stdout.flush)
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
