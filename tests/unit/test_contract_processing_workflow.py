import asyncio
from pathlib import Path
from typing import Any

from contract_processor.application.use_cases.process_contract import ProcessContract
from contract_processor.infrastructure.orchestration.langgraph_workflow import (
    LangGraphWorkflowFactory,
)


DOCUMENT_ID = "a" * 64


class FakePipelines:
    model_name = "fake-mllm"
    prompt_version = "f" * 64

    def __init__(self, *, attribute_catalog_mode: str = "active_catalog") -> None:
        self.calls: list[str] = []
        self.closed = False
        self._attribute_catalog_mode = attribute_catalog_mode

    @property
    def attribute_catalog_mode(self) -> str:
        return self._attribute_catalog_mode

    async def prepare(self, pdf_path: Path) -> dict[str, Any]:
        self.calls.append("prepare")
        return {
            "document_id": DOCUMENT_ID,
            "source_name": pdf_path.name,
            "source_page_count": 1,
        }

    async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
        self.calls.append("core")
        return {"document_id": DOCUMENT_ID, "fields": {"contract_title": {}}}

    async def extract_attributes(
        self, core: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self.calls.append("attribute")
        assert core == {"contract_title": {}}
        return []

    async def extract_clauses(self, pdf_path: Path) -> dict[str, Any]:
        self.calls.append("clause")
        return {"document_id": DOCUMENT_ID, "clauses": [{"heading": "付款"}]}

    async def extract_abstract(self, pdf_path: Path) -> dict[str, Any]:
        self.calls.append("abstract")
        return {"document_id": DOCUMENT_ID, "sections": {}, "text": "摘要"}

    async def schema_versions(self) -> dict[str, str]:
        return {
            "core_schema_version": "1",
            "attribute_schema_version": "0.1",
            "clause_schema_version": "1",
            "summary_schema_version": "1",
        }

    async def close(self) -> None:
        self.closed = True


def test_unified_graph_returns_four_business_outputs(tmp_path: Path) -> None:
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(b"fake-pdf")
    pipelines = FakePipelines()
    use_case = ProcessContract(
        project_root=tmp_path,
        pipelines=pipelines,
        graph_factory=LangGraphWorkflowFactory(),
    )

    result = asyncio.run(use_case.execute(pdf_path))

    assert pipelines.calls[0] == "prepare"
    assert set(pipelines.calls[1:]) == {"core", "attribute", "clause", "abstract"}
    assert pipelines.calls.index("core") < pipelines.calls.index("attribute")
    assert pipelines.closed is True
    assert result.core == {"contract_title": {}}
    assert result.attribute == []
    assert result.clause == [{"heading": "付款"}]
    assert result.abstract.text == "摘要"
    assert "mode" not in result.model_dump(mode="json")
    assert not (tmp_path / "data/runs").exists()


def test_production_graph_skips_attribute_node_for_empty_catalog(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(b"fake-pdf")
    pipelines = FakePipelines(attribute_catalog_mode="empty_catalog")
    use_case = ProcessContract(
        project_root=tmp_path,
        pipelines=pipelines,
        graph_factory=LangGraphWorkflowFactory(),
    )

    result = asyncio.run(use_case.execute(pdf_path))

    assert pipelines.calls[0] == "prepare"
    assert set(pipelines.calls[1:]) == {"core", "clause", "abstract"}
    assert result.attribute == []


def test_production_graph_runs_independent_branches_before_core_finishes(
    tmp_path: Path,
) -> None:
    """Clause/Abstract 只依赖 prepare；Attribute 必须等待 Core 的实际结果。"""

    class ConcurrentProbePipelines(FakePipelines):
        def __init__(self) -> None:
            super().__init__()
            self.core_started = asyncio.Event()
            self.clause_started = asyncio.Event()
            self.abstract_started = asyncio.Event()
            self.attribute_started = asyncio.Event()
            self.release_core = asyncio.Event()

        async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
            self.calls.append("core")
            self.core_started.set()
            await self.release_core.wait()
            return {"document_id": DOCUMENT_ID, "fields": {"contract_title": {}}}

        async def extract_attributes(
            self, core: dict[str, Any]
        ) -> list[dict[str, Any]]:
            self.calls.append("attribute")
            assert core == {"contract_title": {}}
            self.attribute_started.set()
            return []

        async def extract_clauses(self, pdf_path: Path) -> dict[str, Any]:
            self.calls.append("clause")
            self.clause_started.set()
            return {"document_id": DOCUMENT_ID, "clauses": []}

        async def extract_abstract(self, pdf_path: Path) -> dict[str, Any]:
            self.calls.append("abstract")
            self.abstract_started.set()
            return {"document_id": DOCUMENT_ID, "sections": {}, "text": "摘要"}

    async def scenario() -> None:
        pdf_path = tmp_path / "contract.pdf"
        pdf_path.write_bytes(b"fake-pdf")
        pipelines = ConcurrentProbePipelines()
        use_case = ProcessContract(
            project_root=tmp_path,
            pipelines=pipelines,
            graph_factory=LangGraphWorkflowFactory(),
        )
        execution = asyncio.create_task(use_case.execute(pdf_path))

        await asyncio.wait_for(
            asyncio.gather(
                pipelines.core_started.wait(),
                pipelines.clause_started.wait(),
                pipelines.abstract_started.wait(),
            ),
            timeout=1,
        )
        assert pipelines.attribute_started.is_set() is False

        pipelines.release_core.set()
        await asyncio.wait_for(execution, timeout=1)
        assert pipelines.attribute_started.is_set() is True

    asyncio.run(scenario())
