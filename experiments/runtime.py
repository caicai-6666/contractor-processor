"""实验入口共享的异步执行与本地产物保存工具。"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Literal

from contract_processor.async_utils import run_blocking
from contract_processor.infrastructure.extraction.validated_pipelines import (
    ValidatedExtractionPipelines,
)
from contract_processor.settings import load_project_settings


ExperimentStage = Literal["core", "attribute", "clause", "abstract"]


async def run_stage(
    project_root: Path, pdf_path: Path, stage: ExperimentStage
) -> Any:
    """复用正式算法运行单个实验阶段；实验层自行决定是否保存结果。"""

    settings = await load_project_settings(project_root)
    pipelines = ValidatedExtractionPipelines(project_root, settings)
    try:
        await pipelines.prepare(pdf_path)
        if stage == "core":
            return await pipelines.extract_core(pdf_path)
        if stage == "attribute":
            return await pipelines.extract_attributes({})
        if stage == "clause":
            return await pipelines.extract_clauses(pdf_path)
        return await pipelines.extract_abstract(pdf_path)
    finally:
        await pipelines.close()


async def save_result(output_root: Path, payload: Any) -> Path:
    """只有实验代码可以创建带时间戳的本地调试产物。"""

    return await run_blocking(_save_result_sync, output_root, payload)


def _save_result_sync(output_root: Path, payload: Any) -> Path:
    """隔离实验文件写入，避免阻塞实验事件循环。"""

    run_dir = output_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result_path
