import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from contract_processor.application.errors import FieldDiscoveryUnavailableError
from contract_processor.application.schemas.field_discovery import (
    FieldDiscoveryOutput,
    FieldDiscoveryRequest,
)
from contract_processor.bootstrap import container
from contract_processor.domain.enums import FieldKind, RuntimeMode
from contract_processor.domain.models import FieldCatalogSnapshot
from contract_processor.domain.runtime import (
    RuntimeConfigurationError,
    validate_core_catalog_for_mode,
)
from contract_processor.infrastructure.extraction.core import (
    CoreExtractionService,
    EmptyCoreExtractionService,
)
from contract_processor.infrastructure.extraction.attribute import AttributeExtractionService
from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.extraction.validated_pipelines import (
    ValidatedExtractionPipelines,
)
from contract_processor.infrastructure.extraction.stage_result import StageResult
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
from contract_processor.settings import load_project_settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_ID = "a" * 64


def empty_snapshot(kind: FieldKind) -> FieldCatalogSnapshot:
    return FieldCatalogSnapshot(
        kind=kind,
        schema_version="0",
        status="empty",
        definitions=(),
    )


@pytest.mark.parametrize("mode", [RuntimeMode.DISCOVERY])
def test_discovery_mode_accepts_zero_core(mode: RuntimeMode) -> None:
    validate_core_catalog_for_mode(mode, empty_snapshot(FieldKind.CORE))


def test_production_mode_rejects_zero_core() -> None:
    with pytest.raises(RuntimeConfigurationError, match="生产模式必须配置至少一个 Core"):
        validate_core_catalog_for_mode(
            RuntimeMode.PRODUCTION, empty_snapshot(FieldKind.CORE)
        )


def test_empty_core_service_returns_empty_result_without_model_call() -> None:
    context = PdfExtractionContext(
        project_root=PROJECT_ROOT,
        pdf_path=Path("contract.pdf"),
        document_id=DOCUMENT_ID,
        images=[{"page": 1, "data_url": "data:image/png;base64,AA=="}],
        source_page_count=1,
        client=object(),  # type: ignore[arg-type]
    )

    result = asyncio.run(EmptyCoreExtractionService("0").extract(context))

    assert result.payload == {"document_id": DOCUMENT_ID, "fields": {}}
    assert result.validation["mode"] == "empty_catalog"
    assert result.validation["configured_field_count"] == 0
    assert result.metrics == {"model_calls": 0}


def write_catalog(
    path: Path,
    *,
    kind: FieldKind,
    status: str,
    fields: list[dict[str, Any]] | None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": "0",
        "field_set": kind.value,
        "status": status,
    }
    if fields is not None:
        payload["fields"] = fields
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("status", "fields", "message"),
    [
        ("draft", [], "必须显式声明 status=empty"),
        ("empty", [{"field_id": "x"}], "status=empty 时 fields 必须为空"),
        ("empty", None, "fields 必须是数组"),
    ],
)
def test_catalog_rejects_ambiguous_empty_states(
    tmp_path: Path,
    status: str,
    fields: list[dict[str, Any]] | None,
    message: str,
) -> None:
    core_path = tmp_path / "core.yaml"
    attribute_path = tmp_path / "attribute.yaml"
    write_catalog(core_path, kind=FieldKind.CORE, status=status, fields=fields)
    write_catalog(
        attribute_path, kind=FieldKind.ATTRIBUTE, status="empty", fields=[]
    )
    catalog = YamlFieldCatalog(
        core_path=core_path,
        attribute_path=attribute_path,
    )

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(catalog.snapshot(FieldKind.CORE))


def test_current_nonempty_core_catalog_is_valid_for_both_modes() -> None:
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))
    catalog = YamlFieldCatalog(
        core_path=PROJECT_ROOT / settings.paths.core_fields,
        attribute_path=PROJECT_ROOT / settings.paths.attribute_fields,
    )
    snapshot = asyncio.run(catalog.snapshot(FieldKind.CORE))

    assert snapshot.field_count > 0
    assert snapshot.is_empty is False
    validate_core_catalog_for_mode(RuntimeMode.DISCOVERY, snapshot)
    validate_core_catalog_for_mode(RuntimeMode.PRODUCTION, snapshot)


def test_discovery_builder_fails_before_loading_resources_without_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_settings_load(project_root: Path) -> None:
        del project_root
        raise AssertionError("缺少发现实现时不应加载配置或初始化资源")

    monkeypatch.setattr(container, "load_project_settings", unexpected_settings_load)

    with pytest.raises(FieldDiscoveryUnavailableError, match="FieldDiscoveryService"):
        asyncio.run(container.build_discover_contract_fields(PROJECT_ROOT))


def test_production_builder_rejects_zero_core_before_pipeline_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))

    class FakeCatalog:
        def __init__(self, **paths: Path) -> None:
            del paths

        async def snapshot(self, kind: FieldKind) -> FieldCatalogSnapshot:
            return empty_snapshot(kind)

    async def fake_settings(project_root: Path):
        del project_root
        return settings

    monkeypatch.setattr(container, "load_project_settings", fake_settings)
    monkeypatch.setattr(container, "YamlFieldCatalog", FakeCatalog)

    with pytest.raises(RuntimeConfigurationError, match="生产模式必须配置至少一个 Core"):
        asyncio.run(container.build_process_contract(PROJECT_ROOT))


class RecordingDiscoveryService:
    def __init__(self) -> None:
        self.request: FieldDiscoveryRequest | None = None

    async def discover(self, request: FieldDiscoveryRequest) -> FieldDiscoveryOutput:
        self.request = request
        return FieldDiscoveryOutput(candidates=({"candidate_id": "delivery_date"},))


def test_discovery_builder_selects_empty_core_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))

    class FakeCatalog:
        def __init__(self, **paths: Path) -> None:
            del paths

        async def snapshot(self, kind: FieldKind) -> FieldCatalogSnapshot:
            return empty_snapshot(kind)

    async def fake_settings(project_root: Path):
        del project_root
        return settings

    monkeypatch.setattr(container, "load_project_settings", fake_settings)
    monkeypatch.setattr(container, "YamlFieldCatalog", FakeCatalog)

    use_case = asyncio.run(
        container.build_discover_contract_fields(
            PROJECT_ROOT,
            field_discovery_service=RecordingDiscoveryService(),
        )
    )
    pipelines = use_case._pipelines  # type: ignore[attr-defined]

    assert pipelines.core_catalog_mode == "empty_catalog"
    assert isinstance(pipelines._core_service, EmptyCoreExtractionService)


def test_production_builder_selects_attribute_extractor_for_nonempty_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))
    real_catalog = YamlFieldCatalog(
        core_path=PROJECT_ROOT / settings.paths.core_fields,
        attribute_path=PROJECT_ROOT / settings.paths.attribute_fields,
    )
    core_snapshot = asyncio.run(real_catalog.snapshot(FieldKind.CORE))
    attribute_definition = replace(
        core_snapshot.definitions[0], kind=FieldKind.ATTRIBUTE
    )
    attribute_snapshot = FieldCatalogSnapshot(
        kind=FieldKind.ATTRIBUTE,
        schema_version="1",
        status="active",
        definitions=(attribute_definition,),
    )

    class FakeCatalog:
        def __init__(self, **paths: Path) -> None:
            del paths

        async def snapshot(self, kind: FieldKind) -> FieldCatalogSnapshot:
            return core_snapshot if kind is FieldKind.CORE else attribute_snapshot

    async def fake_settings(project_root: Path):
        del project_root
        return settings

    monkeypatch.setattr(container, "load_project_settings", fake_settings)
    monkeypatch.setattr(container, "YamlFieldCatalog", FakeCatalog)

    use_case = asyncio.run(container.build_process_contract(PROJECT_ROOT))
    pipelines = use_case._pipelines  # type: ignore[attr-defined]

    assert pipelines.attribute_catalog_mode == "active_catalog"
    assert isinstance(pipelines._attribute_service, AttributeExtractionService)


def test_discovery_adapter_passes_pdf_and_empty_core_to_discovery_port(
    tmp_path: Path,
) -> None:
    pdf_path = (tmp_path / "contract.pdf").resolve()
    pdf_path.write_bytes(b"fake-pdf")
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))
    snapshot = empty_snapshot(FieldKind.CORE)
    service = RecordingDiscoveryService()
    pipelines = ValidatedExtractionPipelines(
        PROJECT_ROOT,
        settings,
        runtime_mode=RuntimeMode.DISCOVERY,
        core_catalog_snapshot=snapshot,
        field_discovery_service=service,
        core_service=EmptyCoreExtractionService(snapshot.schema_version),
    )
    pipelines._context = PdfExtractionContext(  # type: ignore[assignment]
        project_root=PROJECT_ROOT,
        pdf_path=pdf_path,
        document_id=DOCUMENT_ID,
        images=[
            {
                "page": 1,
                "data_url": "data:image/png;base64,AA==",
                "image_bytes": 1,
            }
        ],
        source_page_count=1,
        client=object(),  # type: ignore[arg-type]
    )

    core = asyncio.run(pipelines.extract_core(pdf_path))
    candidates = asyncio.run(pipelines.discover_fields(pdf_path, core["fields"]))

    assert core["fields"] == {}
    assert candidates == [{"candidate_id": "delivery_date"}]
    assert service.request is not None
    assert service.request.contract_path == pdf_path
    assert service.request.core_result == {}
    assert service.request.core_definitions == ()
    assert {
        definition.field_id for definition in service.request.attribute_definitions
    } == {
        "order_numbers",
        "project_numbers",
        "delivery_commitment",
        "delivery_locations",
        "payment_schedule",
        "invoice_requirement",
        "acceptance_mechanism",
        "warranty_commitment",
        "performance_security",
        "dispute_resolution",
    }
    assert service.request.pages[0].page_number == 1


def test_active_attribute_receives_core_result_and_contract_understanding(
    tmp_path: Path,
) -> None:
    """Core 的内部理解地图只在同一适配器内传给 Attribute，不进入对外 Core payload。"""

    pdf_path = (tmp_path / "contract.pdf").resolve()
    pdf_path.write_bytes(b"fake-pdf")
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))
    catalog = YamlFieldCatalog(
        core_path=PROJECT_ROOT / settings.paths.core_fields,
        attribute_path=PROJECT_ROOT / settings.paths.attribute_fields,
    )
    core_snapshot = asyncio.run(catalog.snapshot(FieldKind.CORE))
    attribute_snapshot = asyncio.run(catalog.snapshot(FieldKind.ATTRIBUTE))

    class FakeCoreService:
        async def extract(self, context: PdfExtractionContext) -> StageResult[dict[str, Any]]:
            assert context.pdf_path == pdf_path
            return StageResult(
                payload={"document_id": DOCUMENT_ID, "fields": {"contract_title": {}}},
                validation={},
                artifacts={"contract_understanding_bullets": "- 页面地图：第 1 页"},
            )

    class RecordingAttributeService:
        def __init__(self) -> None:
            self.core_fields: dict[str, Any] | None = None
            self.understanding: str | None = None

        async def extract(
            self,
            context: PdfExtractionContext,
            *,
            core_fields: dict[str, Any],
            contract_understanding_bullets: str,
        ) -> StageResult[list[dict[str, Any]]]:
            assert context.pdf_path == pdf_path
            self.core_fields = core_fields
            self.understanding = contract_understanding_bullets
            return StageResult(
                payload=[{"field_id": "order_numbers", "status": "not_found"}],
                validation={"is_valid": True, "mode": "active_catalog"},
            )

    attribute_service = RecordingAttributeService()
    pipelines = ValidatedExtractionPipelines(
        PROJECT_ROOT,
        settings,
        runtime_mode=RuntimeMode.PRODUCTION,
        core_catalog_snapshot=core_snapshot,
        attribute_catalog_snapshot=attribute_snapshot,
        core_service=FakeCoreService(),
        attribute_service=attribute_service,
    )
    pipelines._context = PdfExtractionContext(  # type: ignore[assignment]
        project_root=PROJECT_ROOT,
        pdf_path=pdf_path,
        document_id=DOCUMENT_ID,
        images=[
            {
                "page": 1,
                "data_url": "data:image/png;base64,AA==",
                "image_bytes": 1,
            }
        ],
        source_page_count=1,
        client=object(),  # type: ignore[arg-type]
    )

    core = asyncio.run(pipelines.extract_core(pdf_path))
    attribute = asyncio.run(pipelines.extract_attributes(core["fields"]))

    assert core == {"document_id": DOCUMENT_ID, "fields": {"contract_title": {}}}
    assert attribute[0]["field_id"] == "order_numbers"
    assert attribute_service.core_fields == {"contract_title": {}}
    assert attribute_service.understanding == "- 页面地图：第 1 页"


def test_discovery_builder_accepts_current_nonzero_core_when_service_is_injected() -> None:
    use_case = asyncio.run(
        container.build_discover_contract_fields(
            PROJECT_ROOT,
            field_discovery_service=RecordingDiscoveryService(),
        )
    )

    pipelines = use_case._pipelines  # type: ignore[attr-defined]
    assert pipelines.core_catalog_mode == "active_catalog"
    assert pipelines.attribute_catalog_mode == "active_catalog"
    assert isinstance(pipelines._core_service, CoreExtractionService)


def test_production_builder_accepts_current_draft_attribute_catalog() -> None:
    use_case = asyncio.run(container.build_process_contract(PROJECT_ROOT))
    pipelines = use_case._pipelines  # type: ignore[attr-defined]

    assert pipelines.attribute_catalog_mode == "active_catalog"
    assert isinstance(pipelines._attribute_service, AttributeExtractionService)


def test_runtime_settings_default_to_production() -> None:
    settings = asyncio.run(load_project_settings(PROJECT_ROOT))

    assert settings.runtime.mode is RuntimeMode.PRODUCTION
    assert settings.models.mllm.max_concurrent_requests == 3
