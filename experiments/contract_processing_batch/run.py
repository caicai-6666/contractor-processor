#!/usr/bin/env python3
"""保存完整产物的 production 批量回归实验入口。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.application.errors import StageValidationError  # noqa: E402
from contract_processor.bootstrap.container import build_process_contract  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行并保存完整 production 批量回归产物")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/input"),
        help="待测 PDF 目录；相对路径以项目根目录为基准。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/outputs/contract_processing_batch"),
        help="实验输出根目录。",
    )
    return parser.parse_args()


def _write_json_sync(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def async_main() -> Path:
    """逐份新建 production 用例，保存结果或失败原因，避免隐藏批次内资源生命周期。"""

    args = parse_args()
    input_dir = args.input_dir if args.input_dir.is_absolute() else PROJECT_ROOT / args.input_dir
    output_root = args.output_dir if args.output_dir.is_absolute() else PROJECT_ROOT / args.output_dir
    pdf_paths = await run_blocking(lambda: sorted(input_dir.glob("*.pdf")))
    if not pdf_paths:
        raise RuntimeError(f"合同目录中没有 PDF：{input_dir}")

    run_dir = output_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)
    await run_blocking(
        _write_json_sync,
        run_dir / "batch_manifest.json",
        {"input_dir": str(input_dir), "contracts": [path.name for path in pdf_paths]},
    )

    summary: list[dict[str, Any]] = []
    for index, pdf_path in enumerate(pdf_paths, start=1):
        item: dict[str, Any] = {"index": index, "source_name": pdf_path.name}
        try:
            # ProcessContract 在每次 execute 后关闭客户端，故批量实验按合同重建用例。
            use_case = await build_process_contract(PROJECT_ROOT)
            result = await use_case.execute(pdf_path)
            result_name = f"{index:02d}_result.json"
            await run_blocking(_write_json_sync, run_dir / result_name, result.model_dump(mode="json"))
            item.update(
                {
                    "status": "succeeded",
                    "result": result_name,
                    "core_field_count": len(result.core),
                    "attribute_count": len(result.attribute),
                    "clause_count": len(result.clause),
                    "abstract_characters": len(result.abstract.text.strip()),
                }
            )
        except StageValidationError as error:
            # 生产阶段不写诊断；实验边界可保存无 raw response 的校验摘要以支持回归定位。
            diagnostic_name = f"{index:02d}_failure_diagnostic.json"
            await run_blocking(
                _write_json_sync,
                run_dir / diagnostic_name,
                {
                    "stage": error.stage,
                    "validation": error.validation,
                    "metrics": error.metrics,
                },
            )
            item.update(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "diagnostic": diagnostic_name,
                }
            )
        except Exception as error:  # 实验需保留非阶段失败并继续下一份合同。
            item.update(
                {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
            )
        summary.append(item)
        await run_blocking(_write_json_sync, run_dir / "summary.json", summary)

    return run_dir


def main() -> int:
    run_dir = asyncio.run(async_main())
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
