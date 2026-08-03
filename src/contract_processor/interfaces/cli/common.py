"""CLI 测试入口共享的项目定位与版本读取工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from contract_processor.async_utils import run_blocking
from contract_processor.settings import ProjectSettings, load_project_settings


async def resolve_project_root(explicit_root: Path | None) -> Path:
    """定位包含配置和机器规范的项目根目录。

    CLI 允许从项目任意子目录启动，也允许通过 ``--project-root`` 明确指定。不能再根据
    site-packages 的安装位置猜测项目目录，否则 wheel 安装后会读取错误的数据文件。
    """

    return await run_blocking(_resolve_project_root_sync, explicit_root)


def _resolve_project_root_sync(explicit_root: Path | None) -> Path:
    """供工作线程调用的项目根目录探测实现。"""

    if explicit_root is not None:
        candidates: Iterable[Path] = (explicit_root,)
    else:
        current = Path.cwd().resolve()
        source_checkout = Path(__file__).resolve().parents[4]
        candidates = (current, *current.parents, source_checkout)

    checked: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in checked:
            continue
        checked.add(resolved)
        if (
            (resolved / "configs/settings.yaml").is_file()
            and (resolved / "data/definitions").is_dir()
        ):
            return resolved
    requested = str(explicit_root) if explicit_root is not None else str(Path.cwd())
    raise FileNotFoundError(
        f"无法从 {requested} 定位项目根目录；请通过 --project-root 指定。"
    )


async def load_cli_settings(project_root: Path) -> ProjectSettings:
    """加载配置并确认四份机器规范均存在。"""

    settings = await load_project_settings(project_root)
    for relative_path in specification_paths(settings).values():
        path = project_root / relative_path
        if not await run_blocking(path.is_file):
            raise FileNotFoundError(f"机器规范不存在：{path}")
    return settings


def specification_paths(settings: ProjectSettings) -> dict[str, Path]:
    """返回聚合结果中四个规范版本对应的配置路径。"""

    return {
        "core_schema_version": settings.paths.core_fields,
        "attribute_schema_version": settings.paths.attribute_fields,
        "clause_schema_version": settings.paths.clause_fields,
        "summary_schema_version": settings.paths.contract_summary_policy,
    }


async def resolve_from_root(path: Path, project_root: Path) -> Path:
    """将 CLI 的相对路径统一解释为项目根目录相对路径。"""

    candidate = path if path.is_absolute() else project_root / path
    return await run_blocking(candidate.resolve)
