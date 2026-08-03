#!/usr/bin/env python3
"""单文件合同工作流的 CLI/IDE 测试入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from contract_processor.application.schemas.contract_processing import (
    ContractProcessingResult,
)
from contract_processor.application.schemas.field_discovery import FieldDiscoveryResult
from contract_processor.async_utils import run_blocking
from contract_processor.bootstrap.container import build_contract_runtime
from contract_processor.domain.enums import RuntimeMode
from contract_processor.interfaces.cli.common import (
    resolve_from_root,
    resolve_project_root,
)


DEFAULT_PDF_PATH = Path("data/input/大肯科技合同.pdf")
SingleFileResult = ContractProcessingResult | FieldDiscoveryResult


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按显式模式运行单份合同工作流")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_PDF_PATH,
        help="合同 PDF；相对路径以项目根目录为基准。",
    )
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument(
        "--mode",
        type=RuntimeMode,
        choices=[mode.value for mode in RuntimeMode],
        default=None,
        help="运行模式；未指定时读取 settings.runtime.mode。",
    )
    return parser.parse_args()


async def run_single_file(
    pdf_path: Path,
    *,
    project_root: Path | None = None,
    mode: RuntimeMode | None = None,
) -> SingleFileResult:
    """执行一份合同并直接返回应用结果，不产生控制台或文件副作用。"""

    resolved_root = await resolve_project_root(project_root)
    resolved_pdf = await resolve_from_root(pdf_path, resolved_root)
    use_case = await build_contract_runtime(resolved_root, mode=mode)
    return await use_case.execute(resolved_pdf)


async def async_main() -> SingleFileResult:
    """解析 CLI 参数、输出 JSON 并同时将同一个结果返回给调用方。"""

    args = parse_args()
    result = await run_single_file(
        args.pdf,
        project_root=args.project_root,
        mode=args.mode,
    )
    await run_blocking(
        sys.stdout.write,
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
    )
    return result


def main() -> int:
    """控制台脚本的同步外壳；业务执行始终位于异步事件循环中。"""

    asyncio.run(async_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
