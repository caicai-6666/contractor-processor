"""合同处理节点；业务节点不依赖 LangGraph 的具体 API。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from contract_processor.application.workflows.state import ContractProcessingState


class ContractExtractionPipelines(Protocol):
    """四类业务产物的抽取端口，由基础设施适配器实现。"""

    @property
    def model_name(self) -> str:
        """返回当前 MLLM 模型名。"""

    @property
    def prompt_version(self) -> str:
        """返回全部正式 Prompt 内容哈希。"""

    @property
    def attribute_catalog_mode(self) -> str:
        """返回 empty_catalog 或 active_catalog，供生产图决定是否注册节点。"""

    async def prepare(self, pdf_path: Path) -> dict[str, Any]:
        """计算文档身份、渲染页面并建立共享模型会话。"""

    async def extract_core(self, pdf_path: Path) -> dict[str, Any]:
        """提取 Core。"""

    async def extract_attributes(self, core: dict[str, Any]) -> list[dict[str, Any]]:
        """严格按照非空正式 Attribute 目录提取固定字段。"""

    async def extract_clauses(self, pdf_path: Path) -> dict[str, Any]:
        """提取 Clause。"""

    async def extract_abstract(self, pdf_path: Path) -> dict[str, Any]:
        """生成固定格式摘要。"""

    async def schema_versions(self) -> dict[str, str]:
        """返回当前规范源版本。"""

    async def close(self) -> None:
        """异步释放模型连接等共享资源。"""


class ContractProcessingNodes:
    """把外部抽取能力转换为可测试的纯节点返回值。"""

    def __init__(self, pipelines: ContractExtractionPipelines) -> None:
        self._pipelines = pipelines

    async def prepare(self, state: ContractProcessingState) -> dict[str, Any]:
        pdf_path = Path(state["contract_path"])
        prepared = await self._pipelines.prepare(pdf_path)
        return {
            "document_id": prepared["document_id"],
            "source_name": prepared["source_name"],
            "source_page_count": prepared["source_page_count"],
        }

    async def extract_core(self, state: ContractProcessingState) -> dict[str, Any]:
        result = await self._pipelines.extract_core(Path(state["contract_path"]))
        # document_id 是程序身份，不在 Core 业务字段中重复保存。
        return {"core_result": result["fields"]}

    async def extract_attributes(self, state: ContractProcessingState) -> dict[str, Any]:
        return {
            "attribute_result": await self._pipelines.extract_attributes(
                state["core_result"]
            )
        }

    async def extract_clauses(self, state: ContractProcessingState) -> dict[str, Any]:
        result = await self._pipelines.extract_clauses(Path(state["contract_path"]))
        return {"clause_result": result["clauses"]}

    async def extract_abstract(self, state: ContractProcessingState) -> dict[str, Any]:
        result = await self._pipelines.extract_abstract(Path(state["contract_path"]))
        return {
            "abstract_result": {
                "sections": result["sections"],
                "text": result["text"],
            }
        }

    async def finalize(self, state: ContractProcessingState) -> dict[str, Any]:
        return {
            "processing_metadata": {
                "model": self._pipelines.model_name,
                "prompt_version": self._pipelines.prompt_version,
                "source_page_count": state["source_page_count"],
                **await self._pipelines.schema_versions(),
            }
        }
