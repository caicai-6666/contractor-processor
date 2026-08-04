"""字段发现节点；不依赖 LangGraph 的具体 API。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from contract_processor.application.workflows.state import FieldDiscoveryState


class FieldDiscoveryPipelines(Protocol):
    """发现模式在开放发现前先获得固定 Core 与 Attribute 上下文。"""

    @property
    def model_name(self) -> str:
        """返回当前 MLLM 模型名。"""

    @property
    def prompt_version(self) -> str:
        """返回正式 Prompt 内容哈希。"""

    @property
    def core_catalog_mode(self) -> str:
        """返回 empty_catalog 或 active_catalog。"""

    @property
    def attribute_catalog_mode(self) -> str:
        """返回固定 Attribute 目录的 empty_catalog 或 active_catalog。"""

    @property
    def attribute_extraction_metrics(self) -> dict[str, Any]:
        """返回固定 Attribute 成功数、跳过字段与字段级失败诊断。"""

    @property
    def field_discovery_metrics(self) -> dict[str, Any]:
        """返回当前合同候选生成、门禁与身份归并的审计指标。"""

    async def prepare(self, pdf_path: Path) -> dict[str, Any]:
        """计算文档身份、渲染页面并建立共享模型会话。"""

    async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
        """零 Core 时确定性返回空结果，否则执行现有 Core 算法。"""

    async def extract_attributes(self, core: dict[str, Any]) -> list[dict[str, Any]]:
        """抽取固定 Attribute，供候选字段门禁排除既有语义覆盖。"""

    async def discover_fields(
        self,
        pdf_path: Path,
        core: dict[str, Any],
        attributes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """调用注入的字段发现端口。"""

    async def field_schema_versions(self) -> dict[str, str]:
        """只返回 Core 与 Attribute 规范版本。"""

    async def close(self) -> None:
        """释放模型连接等共享资源。"""


class FieldDiscoveryNodes:
    """把发现能力转换为独立状态节点。"""

    def __init__(self, pipelines: FieldDiscoveryPipelines) -> None:
        self._pipelines = pipelines

    async def prepare(self, state: FieldDiscoveryState) -> dict[str, Any]:
        prepared = await self._pipelines.prepare(Path(state["contract_path"]))
        return {
            "document_id": prepared["document_id"],
            "source_name": prepared["source_name"],
            "source_page_count": prepared["source_page_count"],
        }

    async def extract_core(self, state: FieldDiscoveryState) -> dict[str, Any]:
        result = await self._pipelines.extract_core(Path(state["contract_path"]))
        return {"core_result": result["fields"]}

    async def extract_attributes(self, state: FieldDiscoveryState) -> dict[str, Any]:
        attributes = await self._pipelines.extract_attributes(state["core_result"])
        return {"attribute_result": attributes}

    async def discover_fields(self, state: FieldDiscoveryState) -> dict[str, Any]:
        candidates = await self._pipelines.discover_fields(
            Path(state["contract_path"]),
            state["core_result"],
            state["attribute_result"],
        )
        discovery_metrics = self._pipelines.field_discovery_metrics
        discovery_metrics["attribute_extraction"] = (
            self._pipelines.attribute_extraction_metrics
        )
        return {
            "field_candidates": candidates,
            "discovery_metrics": discovery_metrics,
        }

    async def finalize(self, state: FieldDiscoveryState) -> dict[str, Any]:
        versions = await self._pipelines.field_schema_versions()
        return {
            "processing_metadata": {
                "model": self._pipelines.model_name,
                "prompt_version": self._pipelines.prompt_version,
                "source_page_count": state["source_page_count"],
                "core_schema_version": versions["core_schema_version"],
                "attribute_schema_version": versions["attribute_schema_version"],
                "core_catalog_mode": self._pipelines.core_catalog_mode,
                "attribute_catalog_mode": self._pipelines.attribute_catalog_mode,
            }
        }
