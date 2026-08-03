import asyncio
from pathlib import Path

import pytest

from contract_processor.interfaces.cli.common import (
    load_cli_settings,
    resolve_project_root,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_cli_discovers_project_root_from_nested_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(PROJECT_ROOT / "description/fields/core")

    assert asyncio.run(resolve_project_root(None)) == PROJECT_ROOT


def test_all_machine_specifications_live_under_data_definitions() -> None:
    settings = asyncio.run(load_cli_settings(PROJECT_ROOT))
    configured_paths = {
        settings.paths.core_fields,
        settings.paths.attribute_fields,
        settings.paths.discovery_core_fields,
        settings.paths.discovery_attribute_fields,
        settings.paths.clause_fields,
        settings.paths.contract_summary_policy,
    }

    assert configured_paths == {
        Path("data/definitions/core.yaml"),
        Path("data/definitions/attribute.yaml"),
        Path("data/definitions/discovery/core.yaml"),
        Path("data/definitions/discovery/attribute.yaml"),
        Path("data/definitions/clause.yaml"),
        Path("data/definitions/contract_summary.yaml"),
    }
    assert not list((PROJECT_ROOT / "description").rglob("*.yaml"))


def test_formal_cli_does_not_reference_run_artifact_directories() -> None:
    cli_root = PROJECT_ROOT / "src/contract_processor/interfaces/cli"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in cli_root.glob("*.py")
    )

    assert "data/runs" not in source
    assert "contract_result.json" not in source
