import asyncio
from pathlib import Path
from typing import Any

import pytest

from contract_processor.application.use_cases.discover_contract_fields import (
    DiscoverContractFields,
)
from contract_processor.infrastructure.orchestration.langgraph_workflow import (
    LangGraphWorkflowFactory,
)


DOCUMENT_ID = "a" * 64


class FakeDiscoveryPipelines:
    model_name = "fake-mllm"
    prompt_version = "f" * 64

    def __init__(self, core: dict[str, Any]) -> None:
        self.core = core
        self.calls: list[str] = []
        self.closed = False

    @property
    def core_catalog_mode(self) -> str:
        return "empty_catalog" if not self.core else "active_catalog"

    async def prepare(self, pdf_path: Path) -> dict[str, Any]:
        self.calls.append("prepare")
        return {
            "document_id": DOCUMENT_ID,
            "source_name": pdf_path.name,
            "source_page_count": 1,
        }

    async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
        del pdf_path
        self.calls.append("core")
        return {"document_id": DOCUMENT_ID, "fields": self.core}

    async def discover_fields(
        self, pdf_path: Path, core: dict[str, Any]
    ) -> list[dict[str, Any]]:
        del pdf_path
        self.calls.append("discover")
        assert core == self.core
        return [{"candidate_id": "delivery_date"}]

    async def field_schema_versions(self) -> dict[str, str]:
        self.calls.append("versions")
        return {
            "core_schema_version": "1",
            "attribute_schema_version": "0",
        }

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("core", [{}, {"contract_title": {}}])
def test_discovery_graph_supports_zero_and_nonzero_core_without_other_nodes(
    tmp_path: Path, core: dict[str, Any]
) -> None:
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(b"fake-pdf")
    pipelines = FakeDiscoveryPipelines(core)
    use_case = DiscoverContractFields(
        project_root=tmp_path,
        pipelines=pipelines,
        graph_factory=LangGraphWorkflowFactory(),
    )

    result = asyncio.run(use_case.execute(pdf_path))

    assert pipelines.calls == ["prepare", "core", "discover", "versions"]
    assert pipelines.closed is True
    assert result.mode == "discovery"
    assert result.core == core
    assert result.candidates == [{"candidate_id": "delivery_date"}]
    assert result.processing.core_catalog_mode == (
        "empty_catalog" if not core else "active_catalog"
    )
    dumped = result.model_dump(mode="json")
    assert "clause" not in dumped
    assert "abstract" not in dumped
    assert "attribute" not in dumped


def test_discovery_use_case_closes_resources_when_discovery_fails(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(b"fake-pdf")
    pipelines = FakeDiscoveryPipelines({})

    async def fail(pdf_path: Path, core: dict[str, Any]) -> list[dict[str, Any]]:
        del pdf_path, core
        pipelines.calls.append("discover")
        raise RuntimeError("discovery failed")

    pipelines.discover_fields = fail  # type: ignore[method-assign]
    use_case = DiscoverContractFields(
        project_root=tmp_path,
        pipelines=pipelines,
        graph_factory=LangGraphWorkflowFactory(),
    )

    with pytest.raises(RuntimeError, match="discovery failed"):
        asyncio.run(use_case.execute(pdf_path))

    assert pipelines.closed is True
