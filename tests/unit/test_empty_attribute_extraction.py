import asyncio
from pathlib import Path

import pytest
import yaml

from contract_processor.infrastructure.extraction.attribute import (
    EmptyAttributeExtractionService,
)


DOCUMENT_ID = "a" * 64


def write_catalog(path: Path, *, status: str = "empty", fields=None) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "field_set": "attribute",
                "status": status,
                "fields": [] if fields is None else fields,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_empty_attribute_service_returns_validated_empty_result_without_writing(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "attribute.yaml"
    write_catalog(catalog_path)

    result = asyncio.run(
        EmptyAttributeExtractionService(catalog_path).extract(DOCUMENT_ID)
    )

    assert result.payload == []
    assert result.validation == {
        "is_valid": True,
        "mode": "empty_catalog",
        "document_id": DOCUMENT_ID,
        "attribute_schema_version": "0.1",
        "candidate_count": 0,
    }
    assert set(tmp_path.iterdir()) == {catalog_path}


@pytest.mark.parametrize(
    ("status", "fields"),
    [("active", []), ("empty", [{"field_id": "delivery_location"}])],
)
def test_empty_attribute_service_fails_when_catalog_is_no_longer_empty(
    tmp_path: Path, status: str, fields: list[dict[str, str]]
) -> None:
    catalog_path = tmp_path / "attribute.yaml"
    write_catalog(catalog_path, status=status, fields=fields)

    with pytest.raises(RuntimeError, match="当前节点仍使用空实现"):
        asyncio.run(
            EmptyAttributeExtractionService(catalog_path).extract(DOCUMENT_ID)
        )

    assert not (tmp_path / "run").exists()


def test_empty_attribute_service_rejects_non_sha256_identity(tmp_path: Path) -> None:
    catalog_path = tmp_path / "attribute.yaml"
    write_catalog(catalog_path)

    with pytest.raises(ValueError, match="64 位小写 SHA-256"):
        asyncio.run(
            EmptyAttributeExtractionService(catalog_path).extract("contract-number")
        )
