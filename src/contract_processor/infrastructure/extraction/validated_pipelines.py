"""将已验证的 Core、Clause、摘要流程适配到统一工作流端口。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI
import yaml

from contract_processor.application.errors import (
    FieldDiscoveryUnavailableError,
    StageValidationError,
)
from contract_processor.application.ports.contracts import FieldDiscoveryService
from contract_processor.application.prompts.pdf_prefix import compute_prompt_version
from contract_processor.application.schemas.field_discovery import (
    FieldDiscoveryRequest,
    RenderedDocumentPage,
)
from contract_processor.async_utils import run_blocking
from contract_processor.domain.enums import FieldKind, RuntimeMode
from contract_processor.domain.models import FieldCatalogSnapshot
from contract_processor.infrastructure.extraction.abstract import AbstractExtractionService
from contract_processor.infrastructure.extraction.attribute import (
    AttributeExtractionService,
    EmptyAttributeExtractionService,
)
from contract_processor.infrastructure.extraction.clause import ClauseExtractionService
from contract_processor.infrastructure.extraction.context import PdfExtractionContext
from contract_processor.infrastructure.extraction.core import CoreExtractionService
from contract_processor.infrastructure.extraction.stage_result import StageResult
from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
from contract_processor.infrastructure.pdf.document_identity import compute_document_id
from contract_processor.infrastructure.pdf.rendering import _render_pdf_pages_sync
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
from contract_processor.settings import ProjectSettings


class CoreStageService(Protocol):
    """活动 Core 与空 Core 策略共同遵守的最小接口。"""

    async def extract(
        self, context: PdfExtractionContext
    ) -> StageResult[dict[str, Any]]:
        """返回带校验信息的 Core 阶段结果。"""


class AttributeStageService(Protocol):
    """非空正式 Attribute 目录的最小抽取接口。"""

    async def extract(
        self,
        context: PdfExtractionContext,
        *,
        core_fields: dict[str, Any],
        contract_understanding_bullets: str,
    ) -> StageResult[list[dict[str, Any]]]:
        """基于共享 PDF、Core 上下文和理解地图提取固定 Attribute。"""


class ValidatedExtractionPipelines:
    """Adapter：统一共享资源，并调用正式抽取服务。

    该适配器负责模型连接与 PDF 页面资源的生命周期；各子图服务只负责业务算法，
    不依赖实验目录或运行时动态导入。
    """

    def __init__(
        self,
        project_root: Path,
        settings: ProjectSettings,
        *,
        runtime_mode: RuntimeMode = RuntimeMode.PRODUCTION,
        core_catalog_snapshot: FieldCatalogSnapshot | None = None,
        attribute_catalog_snapshot: FieldCatalogSnapshot | None = None,
        field_catalog: YamlFieldCatalog | None = None,
        field_discovery_service: FieldDiscoveryService | None = None,
        core_service: CoreStageService | None = None,
        clause_service: ClauseExtractionService | None = None,
        abstract_service: AbstractExtractionService | None = None,
        attribute_service: AttributeStageService | None = None,
        empty_attribute_service: EmptyAttributeExtractionService | None = None,
    ) -> None:
        self._project_root = project_root
        self._settings = settings
        self._runtime_mode = runtime_mode
        self._core_catalog_snapshot = core_catalog_snapshot
        self._attribute_catalog_snapshot = attribute_catalog_snapshot
        self._field_catalog = field_catalog or YamlFieldCatalog(
            core_path=project_root
            / (
                settings.paths.discovery_core_fields
                if runtime_mode is RuntimeMode.DISCOVERY
                else settings.paths.core_fields
            ),
            attribute_path=project_root
            / (
                settings.paths.discovery_attribute_fields
                if runtime_mode is RuntimeMode.DISCOVERY
                else settings.paths.attribute_fields
            ),
        )
        self._field_discovery_service = field_discovery_service
        self._core_service = core_service or CoreExtractionService()
        self._clause_service = clause_service or ClauseExtractionService()
        self._abstract_service = abstract_service or AbstractExtractionService()
        self._attribute_service = attribute_service or AttributeExtractionService()
        self._empty_attribute_service = (
            empty_attribute_service or EmptyAttributeExtractionService(
            project_root
            / (
                settings.paths.discovery_attribute_fields
                if runtime_mode is RuntimeMode.DISCOVERY
                else settings.paths.attribute_fields
            )
            )
        )
        self._context: PdfExtractionContext | None = None
        self._client: AsyncOpenAI | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._model_request_limiter = ModelRequestLimiter(
            settings.models.mllm.max_concurrent_requests
        )
        self._prompt_version: str | None = None
        self._core_understanding_bullets: str | None = None
        self._field_discovery_metrics: dict[str, Any] = {}

    @property
    def model_name(self) -> str:
        return self._settings.models.mllm.model

    @property
    def prompt_version(self) -> str:
        if self._prompt_version is None:
            raise RuntimeError("必须先执行 prepare，再读取 Prompt 版本。")
        return self._prompt_version

    @property
    def core_catalog_mode(self) -> str:
        """发现结果显式区分 0 Core 和已有 Core 两种合法路径。"""

        if self._core_catalog_snapshot is not None:
            return (
                "empty_catalog"
                if self._core_catalog_snapshot.is_empty
                else "active_catalog"
            )
        return "active_catalog"

    @property
    def attribute_catalog_mode(self) -> str:
        """空 Attribute 在生产图中直接跳过，不调用空提取节点。"""

        if self._attribute_catalog_snapshot is not None:
            return (
                "empty_catalog"
                if self._attribute_catalog_snapshot.is_empty
                else "active_catalog"
            )
        return "empty_catalog"

    @property
    def prepared_context(self) -> PdfExtractionContext:
        """向实验编排暴露只读共享资源，避免再次渲染同一份 PDF。"""

        if self._context is None:
            raise RuntimeError("必须先执行 prepare，再读取共享抽取上下文。")
        return self._context

    async def prepare(self, pdf_path: Path) -> dict[str, Any]:
        """一次性计算身份、渲染所有页面并建立可复用 vLLM 客户端。"""

        if self._context is not None:
            raise RuntimeError("同一抽取适配器实例只能准备一份合同。")
        resolved = await run_blocking(pdf_path.resolve)
        images, page_count = await run_blocking(self._render_pdf, resolved)

        await run_blocking(load_dotenv, self._project_root / ".env")
        self._prompt_version = await compute_prompt_version(self._project_root)
        mllm = self._settings.models.mllm
        http_client = httpx.AsyncClient(
            timeout=mllm.timeout_seconds, trust_env=False
        )
        client = AsyncOpenAI(
            base_url=mllm.base_url,
            api_key=os.getenv(mllm.api_key_env) or "EMPTY",
            http_client=http_client,
        )
        # 在昂贵抽取开始前快速失败，避免进入只有部分结果的状态。
        try:
            await client.models.list()
        except Exception:
            await http_client.aclose()
            raise
        self._http_client = http_client
        self._client = client
        document_id = await compute_document_id(resolved)
        self._context = PdfExtractionContext(
            project_root=self._project_root,
            pdf_path=resolved,
            document_id=document_id,
            images=images,
            source_page_count=page_count,
            client=client,
            model_request_limiter=self._model_request_limiter,
        )
        return {
            "document_id": document_id,
            "source_name": resolved.name,
            "source_page_count": page_count,
            "model": mllm.model,
            "prompt_version": self.prompt_version,
            "shared_rendering": True,
            "attribute_mode": self.attribute_catalog_mode,
            "runtime_mode": self._runtime_mode.value,
        }

    def _render_pdf(self, resolved: Path) -> tuple[list[dict[str, Any]], int]:
        """PyMuPDF 是阻塞 API；调用方负责在线程中运行本方法。"""

        return _render_pdf_pages_sync(
            resolved,
            max_pages=self._settings.models.mllm.vision.max_pages_per_request,
        )

    async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
        result = await self._core_service.extract(await self._shared_context(pdf_path))
        self._require_stage_valid("Core", result.validation, result.metrics)
        # 只在同一合同、同一适配器生命周期内保存定位辅助，避免其进入对外 Core DTO。
        artifact = result.artifacts.get("contract_understanding_bullets")
        if isinstance(artifact, str) and artifact.strip():
            self._core_understanding_bullets = artifact
        elif self.core_catalog_mode != "empty_catalog":
            raise RuntimeError("活动 Core 阶段未提供 Attribute 所需的合同理解地图。")
        return result.payload

    async def extract_attributes(
        self, core: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """当前规范源为空时确定性返回空数组，不向模型注入不存在的字段约束。"""

        if self._context is None:
            raise RuntimeError("必须先执行 prepare，再调用 Attribute 抽取。")
        if self.attribute_catalog_mode == "empty_catalog":
            result = await self._empty_attribute_service.extract(self._context.document_id)
        else:
            understanding_bullets = self._core_understanding_bullets
            if understanding_bullets is None:
                if (
                    self._runtime_mode is RuntimeMode.DISCOVERY
                    and self.core_catalog_mode == "empty_catalog"
                ):
                    # 0 Core 冷启动不应阻断固定 Attribute；事实仍以原始 PDF 为准。
                    understanding_bullets = (
                        "- Discovery Core 目录为空，未生成合同理解地图。\n"
                        "- 请直接依据全部 PDF 页面和当前 Attribute 字段定义判断。"
                    )
                else:
                    raise RuntimeError("Attribute 必须在成功 Core 阶段后执行。")
            result = await self._attribute_service.extract(
                self._context,
                core_fields=core,
                contract_understanding_bullets=understanding_bullets,
            )
        self._require_stage_valid("Attribute", result.validation, result.metrics)
        return result.payload

    async def discover_fields(
        self,
        pdf_path: Path,
        core: dict[str, Any],
        attributes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把共享 PDF、已知字段目录和 Core 结果交给独立发现端口。"""

        if self._runtime_mode is not RuntimeMode.DISCOVERY:
            raise RuntimeError("生产模式禁止调用字段发现服务。")
        if self._field_discovery_service is None:
            raise FieldDiscoveryUnavailableError(
                "字段发现模式尚未配置 FieldDiscoveryService；"
                "本次改造只建立端口与拓扑，不会静默返回空候选。"
            )
        context = await self._shared_context(pdf_path)
        core_snapshot = self._core_catalog_snapshot or await self._field_catalog.snapshot(
            FieldKind.CORE
        )
        attribute_snapshot = (
            self._attribute_catalog_snapshot
            or await self._field_catalog.snapshot(FieldKind.ATTRIBUTE)
        )
        request = FieldDiscoveryRequest(
            document_id=context.document_id,
            contract_path=context.pdf_path,
            pages=tuple(
                RenderedDocumentPage(
                    page_number=int(page["page"]),
                    data_url=str(page["data_url"]),
                    image_bytes=int(page["image_bytes"]),
                )
                for page in context.images
            ),
            core_definitions=core_snapshot.definitions,
            core_result=core,
            attribute_definitions=attribute_snapshot.definitions,
            attribute_result=tuple(attributes),
        )
        output = await self._field_discovery_service.discover(request)
        self._field_discovery_metrics = dict(output.metrics)
        if not all(isinstance(candidate, dict) for candidate in output.candidates):
            raise RuntimeError("字段发现服务必须返回对象形式的候选。")
        return [dict(candidate) for candidate in output.candidates]

    @property
    def field_discovery_metrics(self) -> dict[str, Any]:
        """当前合同 discovery 服务返回的非敏感审计指标。"""

        return dict(self._field_discovery_metrics)

    async def extract_clauses(self, pdf_path: Path) -> dict[str, Any]:
        if self._runtime_mode is not RuntimeMode.PRODUCTION:
            raise RuntimeError("字段发现模式不能调用 Clause。")
        result = await self._clause_service.extract(
            await self._shared_context(pdf_path)
        )
        self._require_stage_valid("Clause", result.validation, result.metrics)
        return result.payload

    async def extract_abstract(self, pdf_path: Path) -> dict[str, Any]:
        if self._runtime_mode is not RuntimeMode.PRODUCTION:
            raise RuntimeError("字段发现模式不能调用 Abstract。")
        result = await self._abstract_service.extract(
            await self._shared_context(pdf_path)
        )
        self._require_stage_valid("Abstract", result.validation, result.metrics)
        return result.payload

    async def schema_versions(self) -> dict[str, str]:
        paths = {
            "core_schema_version": self._settings.paths.core_fields,
            "attribute_schema_version": self._settings.paths.attribute_fields,
            "clause_schema_version": self._settings.paths.clause_fields,
            "summary_schema_version": self._settings.paths.contract_summary_policy,
        }
        values = await asyncio.gather(
            *(
                run_blocking(self._read_schema_version, self._project_root / path)
                for path in paths.values()
            )
        )
        return dict(zip(paths, values, strict=True))

    async def field_schema_versions(self) -> dict[str, str]:
        """发现模式不读取 Clause 或摘要规范。"""

        paths = {
            "core_schema_version": self._settings.paths.discovery_core_fields,
            "attribute_schema_version": self._settings.paths.discovery_attribute_fields,
        }
        values = await asyncio.gather(
            *(
                run_blocking(self._read_schema_version, self._project_root / path)
                for path in paths.values()
            )
        )
        return dict(zip(paths, values, strict=True))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._client = None
        self._http_client = None
        self._context = None
        self._prompt_version = None
        self._core_understanding_bullets = None

    async def _shared_context(self, pdf_path: Path) -> PdfExtractionContext:
        if self._context is None:
            raise RuntimeError("必须先执行 prepare，再调用抽取子图。")
        if self._context.pdf_path != await run_blocking(pdf_path.resolve):
            raise ValueError("抽取 PDF 与 prepare 阶段不是同一文件。")
        return self._context

    @staticmethod
    def _read_schema_version(path: Path) -> str:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "schema_version" not in payload:
            raise RuntimeError(f"机器规范缺少 schema_version：{path}")
        return str(payload["schema_version"])

    @staticmethod
    def _require_stage_valid(
        stage: str,
        validation: dict[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """把算法的细粒度校验提升为正式工作流的硬门禁。"""

        if stage == "Core":
            if validation.get("mode") == "empty_catalog":
                accepted = (
                    validation.get("is_valid") is True
                    and validation.get("configured_field_count") == 0
                )
            else:
                accepted = not any(validation.values())
        elif stage == "Attribute":
            if validation.get("mode") == "empty_catalog":
                accepted = (
                    validation.get("is_valid") is True
                    and validation.get("candidate_count") == 0
                )
            else:
                accepted = validation.get("is_valid") is True
        elif stage == "Clause":
            accepted = validation.get("is_complete") is True and validation.get(
                "is_valid"
            ) is True
        elif stage == "Abstract":
            accepted = validation.get("is_valid") is True
        else:  # pragma: no cover - 仅供内部三个固定阶段调用。
            raise ValueError(f"未知阶段：{stage}")
        if not accepted:
            # 正式路径只在内存中携带诊断；是否写入由实验适配器决定。
            raise StageValidationError(
                stage=stage,
                validation=validation,
                metrics=metrics or {},
            )
