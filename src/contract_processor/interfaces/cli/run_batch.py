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
from contract_processor.bootstrap.container import (
    build_contract_runtime,
    build_discover_fields_from_batch,
)
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
    parser.add_argument(
        "--max-documents",
        type=int,
        default=None,
        help="仅处理排序后的前 N 份 PDF；用于受控回归，不改变目录内容。",
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
    if args.max_documents is not None:
        if args.max_documents < 1:
            raise ValueError("--max-documents 必须至少为 1。")
        pdf_paths = pdf_paths[: args.max_documents]

    selected_mode = args.mode or settings.runtime.mode
    if selected_mode is RuntimeMode.DISCOVERY:
        async def emit_progress(message: str) -> None:
            await run_blocking(sys.stderr.write, message + "\n")
            await run_blocking(sys.stderr.flush)

        await run_blocking(
            sys.stderr.write,
            f"[DISCOVERY] 开始第一阶段：合同数={len(pdf_paths)}\n",
        )
        await run_blocking(sys.stderr.flush)
        batch_use_case = await build_discover_fields_from_batch(
            project_root, emit_progress=emit_progress
        )
        result = await batch_use_case.execute(pdf_paths)
        await run_blocking(
            sys.stderr.write,
            "[DISCOVERY] 两阶段完成："
            f"冻结候选={result.stage_one.candidate_count}；"
            f"回扫任务={result.stage_two.task_count}；"
            f"回扫失败={result.stage_two.failed_task_count}；"
            f"第二阶段={result.stage_two.status}\n",
        )
        await run_blocking(sys.stderr.flush)
        await run_blocking(
            sys.stdout.write,
            result.model_dump_json(indent=2) + "\n",
        )
        await run_blocking(sys.stdout.flush)
        return 0

    use_case = await build_contract_runtime(project_root, mode=selected_mode)
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
    # IDE 直接点击运行时通常不会传入命令行参数；在此集中维护本地调试默认值。
    # 若 IDE Run Configuration 或终端已传参，则完全以外部参数为准。
    IDE_DEFAULT_ARGS = (
        "--mode",
        "discovery",
        "--input-dir",
        "data/input",
        # "--max-documents",
        # "1",
    )
    if len(sys.argv) == 1:
        sys.argv.extend(IDE_DEFAULT_ARGS)
    raise SystemExit(main())
