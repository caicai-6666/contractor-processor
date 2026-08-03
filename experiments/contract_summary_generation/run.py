#!/usr/bin/env python3
"""Abstract 正式算法的独立异步实验入口。"""

import argparse
import asyncio
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.runtime import run_stage, save_result  # noqa: E402
from contract_processor.async_utils import run_blocking  # noqa: E402


DEFAULT_PDF_PATH = Path("data/input/深圳现象光伏科技有限公司4.17(3)_已签章.pdf")


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="运行 Abstract 生成实验")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF_PATH)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/outputs/contract_summary_generation"),
    )
    args = parser.parse_args()
    pdf_path = args.pdf if args.pdf.is_absolute() else PROJECT_ROOT / args.pdf
    output_dir = (
        args.output_dir
        if args.output_dir.is_absolute()
        else PROJECT_ROOT / args.output_dir
    )
    pdf_path = await run_blocking(pdf_path.resolve)
    payload = await run_stage(PROJECT_ROOT, pdf_path, "abstract")
    await run_blocking(print, f"实验结果：{await save_result(output_dir, payload)}")


if __name__ == "__main__":
    asyncio.run(async_main())
