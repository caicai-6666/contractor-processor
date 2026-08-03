#!/usr/bin/env python3
"""基于已完成字段发现候选池，验证组内字段收敛建议。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from contract_processor.async_utils import run_blocking  # noqa: E402
from contract_processor.domain.enums import FieldKind  # noqa: E402
from contract_processor.infrastructure.llm.request_limiter import (  # noqa: E402
    ModelRequestLimiter,
)
from contract_processor.infrastructure.persistence.yaml_field_catalog import (  # noqa: E402
    YamlFieldCatalog,
)
from contract_processor.settings import load_project_settings  # noqa: E402
from experiments.field_discovery_group_consolidation.merger import (  # noqa: E402
    load_group_profiles,
)
from experiments.field_discovery_group_consolidation.service import (  # noqa: E402
    build_refinement_plan,
    refine_candidate_groups,
    run_global_semantic_gate,
)


DEFAULT_OUTPUT_ROOT = Path("experiments/outputs/field_discovery_group_consolidation")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行字段发现候选池的组内字段收敛实验")
    parser.add_argument(
        "--source-run",
        type=Path,
        required=True,
        help="已完成的 field_discovery_stage_one 运行目录，必须包含 candidate_pool.json。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="实验产物根目录。",
    )
    parser.add_argument(
        "--max-members-per-group",
        type=int,
        default=20,
        help="单次治理允许的每组最大字段数；超限会明确失败而不会截断，默认 20。",
    )
    parser.add_argument(
        "--max-validation-retries",
        type=int,
        default=1,
        choices=range(0, 2),
        help="模型输出未通过候选覆盖或字段契约时的语义纠错重试次数，默认 1。",
    )
    return parser.parse_args(argv)


def build_ide_argv(
    *,
    source_run: str,
    output_dir: str,
    max_members_per_group: int,
    max_validation_retries: int,
) -> list[str]:
    """让 IDE 编辑区与 CLI 复用同一套参数解析。"""

    return [
        "--source-run",
        source_run,
        "--output-dir",
        output_dir,
        "--max-members-per-group",
        str(max_members_per_group),
        "--max-validation-retries",
        str(max_validation_retries),
    ]


def _resolve(path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _project_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _load_json_sync(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic_sync(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _append_line_sync(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def _sha256_file_sync(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class StageLogger:
    """实验日志只保留治理动作和指标，不落盘模型原始输出。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def emit(self, message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}"
        await run_blocking(print, line, flush=True)
        await run_blocking(_append_line_sync, self._path, line)


async def async_main(argv: Sequence[str] | None = None) -> tuple[Path, bool]:
    args = parse_args(argv)
    if args.max_members_per_group < 1:
        raise ValueError("--max-members-per-group 必须至少为 1。")

    source_run = _resolve(args.source_run)
    candidate_pool_path = source_run / "candidate_pool.json"
    source_manifest_path = source_run / "manifest.json"
    if not candidate_pool_path.is_file() or not source_manifest_path.is_file():
        raise FileNotFoundError("--source-run 必须包含 candidate_pool.json 和 manifest.json。")
    source_manifest = await run_blocking(_load_json_sync, source_manifest_path)
    if source_manifest.get("run_kind") != "field_discovery_stage_one":
        raise ValueError("--source-run 不是 field_discovery_stage_one 运行目录。")
    if source_manifest.get("failed_document_count") is None:
        raise RuntimeError("来源字段发现运行尚未完成，拒绝读取可能仍在写入的候选池。")
    profiles = load_group_profiles(await run_blocking(_load_json_sync, candidate_pool_path))

    core_catalog_record = source_manifest.get("discovery_core_catalog", {})
    attribute_catalog_record = source_manifest.get("discovery_attribute_catalog", {})
    core_catalog_path = _resolve(Path(str(core_catalog_record.get("path", ""))))
    attribute_catalog_path = _resolve(Path(str(attribute_catalog_record.get("path", ""))))
    if not core_catalog_path.is_file() or not attribute_catalog_path.is_file():
        raise FileNotFoundError("来源运行记录的 Discovery Core/Attribute 目录不可读。")
    fixed_catalog = YamlFieldCatalog(
        core_path=core_catalog_path, attribute_path=attribute_catalog_path
    )
    core_snapshot, attribute_snapshot = await asyncio.gather(
        fixed_catalog.snapshot(FieldKind.CORE),
        fixed_catalog.snapshot(FieldKind.ATTRIBUTE),
    )
    fixed_definitions = (*core_snapshot.definitions, *attribute_snapshot.definitions)

    await run_blocking(load_dotenv, PROJECT_ROOT / ".env")
    settings = await load_project_settings(PROJECT_ROOT)
    output_root = _resolve(args.output_dir)
    run_dir = output_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    await run_blocking(run_dir.mkdir, parents=True, exist_ok=False)
    logger = StageLogger(run_dir / "stage.log")
    manifest_path = run_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "experiment": "field_discovery_group_consolidation",
        "mode": "relation_graph_two_stage_refinement",
        "started_at": datetime.now(UTC).isoformat(),
        "source_field_discovery_run": _project_path(source_run),
        "source_candidate_pool_sha256": await run_blocking(_sha256_file_sync, candidate_pool_path),
        "source_identity_count": sum(len(profile.members) for profile in profiles),
        "source_group_count": len(profiles),
        "model": settings.models.mllm.model,
        "max_members_per_group": args.max_members_per_group,
        "max_validation_retries": args.max_validation_retries,
        "mutates_source_candidate_pool": False,
        "group_refinements": [],
    }
    await run_blocking(_write_json_atomic_sync, manifest_path, manifest)

    mllm = settings.models.mllm
    http_client = httpx.AsyncClient(timeout=mllm.timeout_seconds, trust_env=False)
    client = AsyncOpenAI(
        base_url=mllm.base_url,
        api_key=os.getenv(mllm.api_key_env) or "EMPTY",
        http_client=http_client,
    )
    limiter = ModelRequestLimiter(mllm.max_concurrent_requests)
    try:
        await logger.emit(
            f"组内字段收敛实验启动：来源身份={manifest['source_identity_count']}，"
            f"来源分组={len(profiles)}，并发={limiter.max_concurrent_requests}"
        )

        reports = await refine_candidate_groups(
            profiles=profiles,
            max_members_per_group=args.max_members_per_group,
            max_validation_retries=args.max_validation_retries,
            client=client,
            settings=settings,
            limiter=limiter,
            emit=logger.emit,
        )
        preliminary_plan = build_refinement_plan(profiles=profiles, reports=reports)
        semantic_gate = await run_global_semantic_gate(
            final_fields=preliminary_plan["final_fields"],
            fixed_definitions=fixed_definitions,
            max_validation_retries=args.max_validation_retries,
            client=client,
            settings=settings,
            limiter=limiter,
        )
        plan = build_refinement_plan(
            profiles=profiles, reports=reports, semantic_gate=semantic_gate
        )
        batch_gate_status = plan["batch_field_id_gate"]
        final_fields = plan["final_fields"]
        manifest.update(
            {
                "completed_at": datetime.now(UTC).isoformat(),
                "succeeded_group_count": plan["succeeded_group_count"],
                "failed_group_count": plan["failed_group_count"],
                "final_field_count": plan["final_field_count"],
                "batch_field_id_gate": batch_gate_status,
                "batch_semantic_gate": plan["batch_semantic_gate"],
                "group_refinements": reports,
            }
        )
        await run_blocking(_write_json_atomic_sync, run_dir / "group_refinements.json", reports)
        await run_blocking(
            _write_json_atomic_sync,
            run_dir / "field_definition_drafts.json",
            final_fields,
        )
        await run_blocking(_write_json_atomic_sync, run_dir / "refinement_plan.json", plan)
        await run_blocking(
            _write_json_atomic_sync, run_dir / "global_semantic_gate.json", semantic_gate
        )
        await run_blocking(_write_json_atomic_sync, manifest_path, manifest)
        succeeded = (
            plan["failed_group_count"] == 0
            and batch_gate_status == "passed"
            and plan["batch_semantic_gate"] == "passed"
        )
        await logger.emit(
            f"实验结束：成功组={plan['succeeded_group_count']}，失败组={plan['failed_group_count']}，"
            f"最终字段={plan['final_field_count']}，批次字段 ID 门禁={batch_gate_status}"
            f"，全局语义门禁={plan['batch_semantic_gate']}"
        )
        return run_dir, succeeded
    finally:
        await client.close()
        if not http_client.is_closed:
            await http_client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    run_dir, succeeded = asyncio.run(async_main(argv))
    print(run_dir)
    return 0 if succeeded else 1


if __name__ == "__main__":
    # IDE Run Configuration 未传参数时，编辑此区即可；显式 CLI 参数始终优先。
    IDE_SOURCE_RUN = "experiments/outputs/field_discovery_stage_one/20260803T043749915744Z"
    IDE_OUTPUT_DIR = "experiments/outputs/field_discovery_group_consolidation"
    IDE_MAX_MEMBERS_PER_GROUP = 20
    IDE_MAX_VALIDATION_RETRIES = 1

    supplied_argv = sys.argv[1:]
    if supplied_argv:
        raise SystemExit(main(supplied_argv))
    raise SystemExit(
        main(
            build_ide_argv(
                source_run=IDE_SOURCE_RUN,
                output_dir=IDE_OUTPUT_DIR,
                max_members_per_group=IDE_MAX_MEMBERS_PER_GROUP,
                max_validation_retries=IDE_MAX_VALIDATION_RETRIES,
            )
        )
    )
