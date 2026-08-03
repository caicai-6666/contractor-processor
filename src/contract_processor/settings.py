"""项目统一配置模型与加载入口。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from contract_processor.async_utils import run_blocking
from contract_processor.domain.enums import RuntimeMode


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationSettings(SettingsModel):
    temperature: float
    top_p: float
    top_k: int
    presence_penalty: float
    repetition_penalty: float
    seed: int
    max_completion_tokens: int = Field(gt=0)


class VisionSettings(SettingsModel):
    max_pages_per_request: int = Field(gt=0)


class MllmSettings(SettingsModel):
    provider: str
    base_url: str
    api_key_env: str
    model: str
    endpoint: str
    timeout_seconds: float = Field(gt=0)
    max_concurrent_requests: int = Field(default=3, ge=1)
    context_window_tokens: int = Field(gt=0)
    generation: GenerationSettings
    vision: VisionSettings


class EmbeddingSettings(SettingsModel):
    """本地多模态 Embedding 服务配置。"""

    provider: str
    base_url: str
    api_key_env: str
    model: str
    endpoint: str
    timeout_seconds: float = Field(gt=0)
    batch_size: int = Field(gt=0)
    max_concurrent_requests: int = Field(default=3, ge=1)
    dimensions: int = Field(gt=0)
    normalize: bool = True


class ModelsSettings(SettingsModel):
    mllm: MllmSettings
    # Embedding 不参与 production 内容提取；入库和 discovery 字段召回按各自边界使用。
    embedding: EmbeddingSettings
    reranker: dict[str, object]


class PathsSettings(SettingsModel):
    input_contracts: Path
    core_fields: Path
    attribute_fields: Path
    # Discovery 与 production 使用不同目录快照，防止试验候选污染正式字段定义。
    discovery_core_fields: Path
    discovery_attribute_fields: Path
    clause_fields: Path
    contract_summary_policy: Path
    contract_embedding_policy: Path
    source_documents: Path
    runtime_data: Path


class ProcessingSettings(SettingsModel):
    persist_page_artifacts: bool = False


class RuntimeSettings(SettingsModel):
    """CLI 未显式指定时采用的工作流模式。"""

    mode: RuntimeMode = RuntimeMode.PRODUCTION


class IngestionSettings(SettingsModel):
    """独立入库子图的业务配置；具体公司名称仍只存在环境变量中。"""

    own_company_names_env: str = Field(
        default="CONTRACT_PROCESSOR_OWN_COMPANY_NAMES", min_length=1
    )


class ElasticsearchSettings(SettingsModel):
    hosts: list[str] = Field(min_length=1)
    username_env: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    ca_certs: Path | None = None
    verify_certs: bool = True
    index_name: str = Field(min_length=1)
    ingestion_experiment_index_name: str = Field(
        default="contracts-ingestion-experiment-v1", min_length=1
    )
    vector_dimensions: int | None = Field(default=None, gt=0)
    number_of_shards: int = Field(default=1, ge=1)
    number_of_replicas: int = Field(default=0, ge=0)


class ProjectSettings(SettingsModel):
    models: ModelsSettings
    paths: PathsSettings
    processing: ProcessingSettings
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    elasticsearch: ElasticsearchSettings

    @model_validator(mode="after")
    def validate_vector_dimensions(self) -> "ProjectSettings":
        configured = self.elasticsearch.vector_dimensions
        if configured is None:
            raise ValueError("正式入库要求配置 elasticsearch.vector_dimensions。")
        if configured != self.models.embedding.dimensions:
            raise ValueError(
                "elasticsearch.vector_dimensions 必须与 "
                "models.embedding.dimensions 一致。"
            )
        return self


async def load_project_settings(project_root: Path) -> ProjectSettings:
    """异步加载并校验唯一 YAML 配置源。"""

    return await run_blocking(_load_project_settings_sync, project_root)


def _load_project_settings_sync(project_root: Path) -> ProjectSettings:
    """供受控工作线程调用的同步 YAML 解析实现。"""

    settings_path = project_root / "configs/settings.yaml"
    with settings_path.open(encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    return ProjectSettings.model_validate(payload)
