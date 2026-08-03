#!/usr/bin/env python3
"""批量生成专家终审后的待入库 Mock 包络。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contract_processor.application.errors import StageValidationError  # noqa: E402
from contract_processor.application.schemas.contract_processing import (  # noqa: E402
    ContractProcessingResult,
)
from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.domain.enums import RuntimeMode  # noqa: E402
from contract_processor.interfaces.api.schemas.contracts import (  # noqa: E402
    ContractProcessResponse,
    ContractReviewConfirmation,
    ContractReviewTrace,
)
from contract_processor.interfaces.cli.run_single_file import (  # noqa: E402
    run_single_file,
)


DEFAULT_REVIEWER = "Jason"
DEFAULT_COMMENT = "实验脚本自动构造的终审确认，仅用于入库模块开发与测试。"


@dataclass(frozen=True, slots=True)
class MockRunOutcome:
    """实验进程退出所需的最小结果，不把 manifest 再读回内存。"""

    run_dir: Path
    failure_count: int


def _parse_aware_datetime(value: str) -> datetime:
    """解析带时区时间，拒绝无法追溯时区的 Mock 审核时间。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"无效 ISO 8601 时间：{value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--reviewed-at 必须包含时区，例如 +08:00 或 Z。")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量生成终审待入库 Mock JSON 包络")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/input"),
        help="待处理 PDF 目录；递归查找，且相对路径以项目根目录为基准。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/outputs/contract_ingestion_mock"),
        help="实验输出根目录；相对路径以项目根目录为基准。",
    )
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER, help="伪造的审核员。")
    parser.add_argument("--comment", default=DEFAULT_COMMENT, help="伪造的审核意见。")
    parser.add_argument(
        "--reviewed-at",
        type=_parse_aware_datetime,
        default=None,
        help="带时区的 ISO 8601 审核时间；省略时使用本次运行的 UTC 时间。",
    )
    return parser.parse_args()


def _write_json_atomic_sync(path: Path, payload: Any) -> None:
    """先写同目录临时文件再替换，避免中断留下半个 JSON。"""

    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _manifest_path(path: Path) -> str:
    """项目内文件使用相对路径，外部显式输入保留绝对路径。"""

    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def build_mock_confirmation(
    result: ContractProcessingResult,
    *,
    reviewer: str,
    reviewed_at: datetime,
    comment: str,
) -> ContractReviewConfirmation:
    """用正式 API DTO 包装提取结果，保证 Mock 与未来前端提交协议一致。"""

    response = ContractProcessResponse.model_validate(result.model_dump(mode="python"))
    return ContractReviewConfirmation(
        document_id=result.document_id,
        review=ContractReviewTrace(
            reviewer=reviewer,
            reviewed_at=reviewed_at,
            comment=comment,
        ),
        result=response,
    )


async def async_main() -> MockRunOutcome:
    args = parse_args()
    input_dir = args.input_dir if args.input_dir.is_absolute() else PROJECT_ROOT / args.input_dir
    output_root = (
        args.output_dir if args.output_dir.is_absolute() else PROJECT_ROOT / args.output_dir
    )
    input_dir = await run_blocking(input_dir.resolve)
    output_root = await run_blocking(output_root.resolve)
    pdf_paths = await run_blocking(
        lambda: sorted(
            (
                path
                for path in input_dir.rglob("*")
                if path.is_file() and path.suffix.lower() == ".pdf"
            ),
            key=lambda path: str(path.relative_to(input_dir)),
        )
    )
    if not pdf_paths:
        raise RuntimeError(f"合同目录中没有 PDF：{input_dir}")

    started_at = datetime.now(UTC)
    reviewed_at = args.reviewed_at or started_at
    review = ContractReviewTrace(
        reviewer=args.reviewer,
        reviewed_at=reviewed_at,
        comment=args.comment,
    )
    run_dir = output_root / started_at.strftime("%Y%m%dT%H%M%S%fZ")
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "experiment": "contract_ingestion_mock",
        "created_at": started_at.isoformat(),
        "input_dir": _manifest_path(input_dir),
        "review": review.model_dump(mode="json"),
        "contracts": [],
    }
    manifest_path = run_dir / "manifest.json"
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)

    failure_count = 0
    for index, pdf_path in enumerate(pdf_paths, start=1):
        item: dict[str, Any] = {
            "index": index,
            "source_name": pdf_path.name,
            "source_pdf": _manifest_path(pdf_path),
        }
        try:
            # 每份合同通过可返回结果的单文件入口重建并关闭自身模型客户端。
            extracted = await run_single_file(
                pdf_path,
                project_root=PROJECT_ROOT,
                mode=RuntimeMode.PRODUCTION,
            )
            if not isinstance(extracted, ContractProcessingResult):
                raise TypeError("待入库 Mock 只接受 production 的合同处理结果。")
            confirmation = build_mock_confirmation(
                extracted,
                reviewer=review.reviewer,
                reviewed_at=review.reviewed_at,
                comment=review.comment,
            )
            package_name = f"{index:02d}_ingestion_package.json"
            await run_blocking(
                _write_json_atomic_sync,
                run_dir / package_name,
                confirmation.model_dump(mode="json"),
            )
            item.update(
                {
                    "status": "succeeded",
                    "document_id": confirmation.document_id,
                    "package": package_name,
                }
            )
        except StageValidationError as error:
            failure_count += 1
            diagnostic_name = f"{index:02d}_failure_diagnostic.json"
            await run_blocking(
                _write_json_atomic_sync,
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
        except Exception as error:  # 批量准备应记录单份失败并继续生成其余 Mock。
            failure_count += 1
            item.update(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
        manifest["contracts"].append(item)
        await run_blocking(_write_json_atomic_sync, manifest_path, manifest)

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["succeeded_count"] = len(pdf_paths) - failure_count
    manifest["failure_count"] = failure_count
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
    return MockRunOutcome(run_dir=run_dir, failure_count=failure_count)


def main() -> int:
    outcome = asyncio.run(async_main())
    print(outcome.run_dir)
    return 1 if outcome.failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
