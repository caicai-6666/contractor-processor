import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from contract_processor.application.errors import StageValidationError
from contract_processor.infrastructure.extraction.validated_pipelines import (
    ValidatedExtractionPipelines,
)
from contract_processor.infrastructure.extraction.stage_result import StageResult
from contract_processor.settings import load_project_settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingAttributeService:
    def __init__(self) -> None:
        self.call: tuple[str, Path] | None = None

    async def extract(self, document_id: str) -> StageResult[list[dict[str, object]]]:
        self.call = (document_id, Path("unused"))
        return StageResult(
            payload=[],
            validation={
                "is_valid": True,
                "mode": "empty_catalog",
                "candidate_count": 0,
            },
        )


@pytest.mark.parametrize(
    ("stage", "validation"),
    [
        (
            "Core",
            {
                "missing_field_ids": [],
                "unexpected_field_ids": [],
                "invalid_field_envelopes": [],
                "required_field_violations": [],
            },
        ),
        ("Clause", {"is_complete": True, "is_valid": True}),
        ("Abstract", {"is_valid": True}),
    ],
)
def test_formal_pipeline_accepts_only_valid_stage_outputs(
    stage: str, validation: dict[str, object]
) -> None:
    ValidatedExtractionPipelines._require_stage_valid(stage, validation)


@pytest.mark.parametrize(
    ("stage", "validation"),
    [
        ("Core", {"missing_field_ids": ["contract_title"]}),
        ("Clause", {"is_complete": False, "is_valid": False}),
        ("Abstract", {"is_valid": False}),
    ],
)
def test_formal_pipeline_rejects_failed_stage_outputs(
    stage: str, validation: dict[str, object]
) -> None:
    with pytest.raises(StageValidationError, match=f"{stage} 阶段校验未通过") as raised:
        ValidatedExtractionPipelines._require_stage_valid(stage, validation)
    assert raised.value.stage == stage
    assert raised.value.validation == validation
    assert raised.value.metrics == {}


def test_formal_attribute_node_delegates_to_injected_empty_service(
    tmp_path: Path,
) -> None:
    service = RecordingAttributeService()
    pipelines = ValidatedExtractionPipelines(
        PROJECT_ROOT,
        asyncio.run(load_project_settings(PROJECT_ROOT)),
        empty_attribute_service=service,  # type: ignore[arg-type]
    )
    document_id = "a" * 64
    # 本测试只验证 Attribute 接线，不应为此建立模型连接或渲染 PDF。
    pipelines._context = SimpleNamespace(document_id=document_id)  # type: ignore[assignment]

    result = asyncio.run(pipelines.extract_attributes({}))

    assert result == []
    assert service.call == (document_id, Path("unused"))
