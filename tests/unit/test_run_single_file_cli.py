import asyncio
import importlib
from pathlib import Path

from contract_processor.application.schemas.contract_processing import (
    ContractAbstract,
    ContractProcessingResult,
    ProcessingMetadata,
)
from contract_processor.domain.enums import RuntimeMode


run_single_module = importlib.import_module(
    "contract_processor.interfaces.cli.run_single_file"
)


def _result() -> ContractProcessingResult:
    return ContractProcessingResult(
        document_id="a" * 64,
        source_name="contract.pdf",
        core={},
        attribute=[],
        clause=[],
        abstract=ContractAbstract(sections={}, text="摘要"),
        processing=ProcessingMetadata(
            model="mllm",
            prompt_version="f" * 64,
            source_page_count=1,
            core_schema_version="1",
            attribute_schema_version="1",
            clause_schema_version="1",
            summary_schema_version="1",
        ),
    )


def test_run_single_file_returns_application_result(monkeypatch) -> None:
    expected = _result()
    captured: dict[str, object] = {}

    class FakeUseCase:
        async def execute(self, pdf_path: Path) -> ContractProcessingResult:
            captured["pdf_path"] = pdf_path
            return expected

    async def fake_resolve_project_root(explicit_root: Path | None) -> Path:
        return Path("/project")

    async def fake_resolve_from_root(path: Path, project_root: Path) -> Path:
        return project_root / path

    async def fake_build_contract_runtime(
        project_root: Path,
        *,
        mode: RuntimeMode | None = None,
    ) -> FakeUseCase:
        captured["project_root"] = project_root
        captured["mode"] = mode
        return FakeUseCase()

    monkeypatch.setattr(
        run_single_module, "resolve_project_root", fake_resolve_project_root
    )
    monkeypatch.setattr(run_single_module, "resolve_from_root", fake_resolve_from_root)
    monkeypatch.setattr(
        run_single_module, "build_contract_runtime", fake_build_contract_runtime
    )

    actual = asyncio.run(
        run_single_module.run_single_file(
            Path("data/input/contract.pdf"),
            project_root=Path("/project"),
            mode=RuntimeMode.PRODUCTION,
        )
    )

    assert actual is expected
    assert captured == {
        "project_root": Path("/project"),
        "mode": RuntimeMode.PRODUCTION,
        "pdf_path": Path("/project/data/input/contract.pdf"),
    }
