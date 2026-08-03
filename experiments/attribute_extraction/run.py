#!/usr/bin/env python3
"""Attribute 空节点的独立实验入口；不调用模型。"""

from __future__ import annotations

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Attribute 空节点实验")
    parser.add_argument("--pdf", type=Path, default=None, help="用于计算 document_id 的合同 PDF。")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/outputs/attribute_extraction"),
        help="实验输出根目录。",
    )
    return parser.parse_args()


async def async_main() -> list[dict[str, object]]:
    args = parse_args()
    pdf_path = args.pdf or DEFAULT_PDF_PATH
    if not pdf_path.is_absolute():
        pdf_path = PROJECT_ROOT / pdf_path
    pdf_path = await run_blocking(pdf_path.resolve)
    if not await run_blocking(pdf_path.is_file):
        raise FileNotFoundError(f"找不到待处理 PDF：{pdf_path}")

    output_root = args.output_dir
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    result = await run_stage(PROJECT_ROOT, pdf_path, "attribute")
    await run_blocking(
        print,
        f"Attribute 空实验结果：{await save_result(output_root, result)}",
    )
    return result


def main() -> list[dict[str, object]]:
    return asyncio.run(async_main())


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"实验失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
