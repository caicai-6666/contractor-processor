"""CLI、未来 API 与后台 Worker 共用的依赖组装入口。"""

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from dotenv import load_dotenv
from elasticsearch import AsyncElasticsearch

from contract_processor.application.ports.contracts import FieldDiscoveryService
from contract_processor.application.prompts.pdf_prefix import compute_prompt_version
from contract_processor.application.schemas.field_discovery import (
    FieldDiscoveryBatchProcessingMetadata,
)
from contract_processor.application.use_cases.discover_contract_fields import (
    DiscoverContractFields,
)
from contract_processor.application.use_cases.discover_fields_from_batch import (
    DiscoverFieldsFromBatch,
)
from contract_processor.application.use_cases.inspect_field_catalog import InspectFieldCatalog
from contract_processor.application.use_cases.ingest_reviewed_contract import (
    IngestReviewedContract,
)
from contract_processor.application.use_cases.process_contract import ProcessContract
from contract_processor.application.services.contract_ingestion_projection import (
    parse_own_company_names,
)
from contract_processor.async_utils import run_blocking
from contract_processor.application.workflows.contract_ingestion import (
    ContractIngestionWorkflow,
)
from contract_processor.application.workflows.contract_processing import (
    ContractExtractionPipelines,
)
from contract_processor.domain.enums import FieldKind, RuntimeMode
from contract_processor.domain.runtime import (
    RuntimeConfigurationError,
    validate_core_catalog_for_mode,
)
from contract_processor.infrastructure.extraction.core import (
    CoreExtractionService,
    EmptyCoreExtractionService,
)
from contract_processor.infrastructure.extraction.validated_pipelines import (
    ValidatedExtractionPipelines,
)
from contract_processor.infrastructure.field_discovery import (
    CandidateFieldExtractionService,
    StructuredFieldDiscoveryService,
)
from contract_processor.infrastructure.embedding import (
    Qwen3VLEmbeddingClient,
    load_contract_embedding_policy,
)
from contract_processor.infrastructure.orchestration.contract_ingestion_graph import (
    ContractIngestionGraphFactory,
)
from contract_processor.infrastructure.orchestration.langgraph_workflow import (
    LangGraphWorkflowFactory,
)
from contract_processor.infrastructure.pdf.document_identity import (
    Sha256DocumentIdentityProvider,
    compute_document_id,
)
from contract_processor.infrastructure.persistence.elasticsearch_contract_index import (
    Elasticsearch9ContractVectorStore,
    ElasticsearchContractIndexRepository,
)
from contract_processor.infrastructure.persistence.local_source_document_store import (
    LocalSourceDocumentStore,
)
from contract_processor.infrastructure.persistence.yaml_field_discovery_result_store import (
    YamlFieldDiscoveryResultStore,
)
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
from contract_processor.settings import ProjectSettings, load_project_settings


async def build_inspect_field_catalog(project_root: Path) -> InspectFieldCatalog:
    """组装不依赖模型服务的字段库检查用例。"""

    settings = await load_project_settings(project_root)
    catalog = YamlFieldCatalog(
        core_path=project_root / settings.paths.core_fields,
        attribute_path=project_root / settings.paths.attribute_fields,
    )
    return InspectFieldCatalog(catalog)


async def build_process_contract(
    project_root: Path,
    *,
    pipelines_transform: Callable[
        [ContractExtractionPipelines], ContractExtractionPipelines
    ]
    | None = None,
) -> ProcessContract:
    """显式组装生产用例；可在应用端口外包裹观测适配器。"""

    settings = await load_project_settings(project_root)
    pipelines = await _build_pipelines(
        project_root=project_root,
        settings=settings,
        mode=RuntimeMode.PRODUCTION,
        field_discovery_service=None,
    )
    resolved_pipelines = (
        pipelines_transform(pipelines)
        if pipelines_transform is not None
        else pipelines
    )
    return ProcessContract(
        project_root=project_root,
        pipelines=resolved_pipelines,
        graph_factory=LangGraphWorkflowFactory(),
    )


def _resolve_project_path(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else project_root / path


async def build_ingest_reviewed_contract(
    project_root: Path,
    *,
    index_name: str | None = None,
    source_document_root: Path | None = None,
) -> IngestReviewedContract:
    """组装与合同提取主图无连接的正式四节点入库用例。"""

    settings = await load_project_settings(project_root)
    embedding_settings = settings.models.embedding
    embedding_policy = await load_contract_embedding_policy(
        _resolve_project_path(project_root, settings.paths.contract_embedding_policy)
    )
    own_company_names_env = settings.ingestion.own_company_names_env
    own_company_names = parse_own_company_names(
        os.getenv(own_company_names_env),
        env_name=own_company_names_env,
    )

    username = os.getenv(settings.elasticsearch.username_env)
    password = os.getenv(settings.elasticsearch.password_env)
    if not username or not password:
        raise RuntimeError("Elasticsearch 用户名或密码环境变量未配置。")
    ca_certs = settings.elasticsearch.ca_certs
    if ca_certs is not None:
        ca_certs = _resolve_project_path(project_root, ca_certs)
    es_client = AsyncElasticsearch(
        settings.elasticsearch.hosts,
        basic_auth=(username, password),
        ca_certs=str(ca_certs) if ca_certs is not None else None,
        verify_certs=settings.elasticsearch.verify_certs,
        request_timeout=60,
    )
    embedding_client = Qwen3VLEmbeddingClient(
        base_url=embedding_settings.base_url,
        api_key=os.getenv(embedding_settings.api_key_env) or "",
        model=embedding_settings.model,
        endpoint=embedding_settings.endpoint,
        timeout_seconds=embedding_settings.timeout_seconds,
        dimensions=embedding_settings.dimensions,
        max_concurrent_requests=embedding_settings.max_concurrent_requests,
        normalize=embedding_settings.normalize,
        policy=embedding_policy,
    )
    vector_store = Elasticsearch9ContractVectorStore(
        client=es_client,
        index_name=index_name or settings.elasticsearch.index_name,
        dimensions=embedding_settings.dimensions,
        number_of_shards=settings.elasticsearch.number_of_shards,
        number_of_replicas=settings.elasticsearch.number_of_replicas,
    )
    index_repository = ElasticsearchContractIndexRepository(vector_store)
    configured_root = _resolve_project_path(
        project_root,
        source_document_root or settings.paths.source_documents,
    )
    workflow = ContractIngestionWorkflow(
        own_company_names=own_company_names,
        identity_provider=Sha256DocumentIdentityProvider(),
        embedding_client=embedding_client,
        source_document_store=LocalSourceDocumentStore(configured_root),
        index_repository=index_repository,
    )
    return IngestReviewedContract(
        workflow=workflow,
        graph_factory=ContractIngestionGraphFactory(),
        embedding_client=embedding_client,
        index_repository=index_repository,
    )


async def build_discover_contract_fields(
    project_root: Path,
    *,
    field_discovery_service: FieldDiscoveryService | None = None,
) -> DiscoverContractFields:
    """组装单合同发现用例；默认使用正式第一阶段服务。"""

    settings = await load_project_settings(project_root)
    resolved_service = field_discovery_service
    close_discovery_service: Callable[[], Awaitable[None]] | None = None
    if resolved_service is None:
        embedding_client = await _build_field_discovery_embedding_client(
            project_root=project_root, settings=settings
        )
        resolved_service = StructuredFieldDiscoveryService(
            project_root=project_root,
            settings=settings,
            embedding_client=embedding_client,
        )
        close_discovery_service = resolved_service.close
    pipelines = await _build_pipelines(
        project_root=project_root,
        settings=settings,
        mode=RuntimeMode.DISCOVERY,
        field_discovery_service=resolved_service,
    )
    return DiscoverContractFields(
        project_root=project_root,
        pipelines=pipelines,
        graph_factory=LangGraphWorkflowFactory(),
        close_discovery_service=close_discovery_service,
    )


async def build_discover_fields_from_batch(
    project_root: Path,
    *,
    emit_progress: Callable[[str], Awaitable[None]] | None = None,
) -> DiscoverFieldsFromBatch:
    """组装批次 discovery 父图，并使所有合同复用同一候选准入限流器。"""

    settings = await load_project_settings(project_root)
    discovery_catalog = YamlFieldCatalog(
        core_path=project_root / settings.paths.discovery_core_fields,
        attribute_path=project_root / settings.paths.discovery_attribute_fields,
    )
    core_snapshot = await discovery_catalog.snapshot(FieldKind.CORE)
    attribute_snapshot = await discovery_catalog.snapshot(FieldKind.ATTRIBUTE)
    prompt_version = await compute_prompt_version(project_root)
    embedding_client = await _build_field_discovery_embedding_client(
        project_root=project_root, settings=settings
    )
    shared_service = StructuredFieldDiscoveryService(
        project_root=project_root,
        settings=settings,
        embedding_client=embedding_client,
    )
    processing_metadata = FieldDiscoveryBatchProcessingMetadata(
        model=settings.models.mllm.model,
        prompt_version=prompt_version,
        embedding_model=embedding_client.model,
        embedding_dimensions=embedding_client.dimensions,
        embedding_instruction_version=(
            embedding_client.field_summary_instruction_version
        ),
        core_schema_version=core_snapshot.schema_version,
        attribute_schema_version=attribute_snapshot.schema_version,
        core_catalog_mode=(
            "empty_catalog" if core_snapshot.is_empty else "active_catalog"
        ),
        attribute_catalog_mode=(
            "empty_catalog"
            if attribute_snapshot.is_empty
            else "active_catalog"
        ),
        max_candidates_per_document=(
            shared_service.max_candidates_per_document
        ),
        top_k=shared_service.top_k,
    )
    candidate_extractor = CandidateFieldExtractionService(
        project_root=project_root,
        settings=settings,
    )

    async def discover_one(pdf_path: Path):
        pipelines = await _build_pipelines(
            project_root=project_root,
            settings=settings,
            mode=RuntimeMode.DISCOVERY,
            field_discovery_service=shared_service,
        )
        use_case = DiscoverContractFields(
            project_root=project_root,
            pipelines=pipelines,
            graph_factory=LangGraphWorkflowFactory(),
        )
        return await use_case.execute(pdf_path)

    return DiscoverFieldsFromBatch(
        project_root=project_root,
        discover_one=discover_one,
        consolidate_candidates=shared_service.consolidate,
        extract_candidate_field=candidate_extractor.extract,
        identify_document=compute_document_id,
        close_stage_one=shared_service.close,
        close_candidate_extractor=candidate_extractor.close,
        graph_factory=LangGraphWorkflowFactory(),
        processing_metadata=processing_metadata,
        result_store=YamlFieldDiscoveryResultStore(
            project_root / "data/definitions/discovery/result"
        ),
        emit_progress=emit_progress,
    )


async def build_contract_runtime(
    project_root: Path,
    *,
    mode: RuntimeMode | None = None,
    field_discovery_service: FieldDiscoveryService | None = None,
) -> ProcessContract | DiscoverContractFields:
    """按显式参数或配置默认值构造唯一合法的运行方式。"""

    settings = await load_project_settings(project_root)
    selected_mode = mode or settings.runtime.mode
    if selected_mode is RuntimeMode.PRODUCTION:
        pipelines = await _build_pipelines(
            project_root=project_root,
            settings=settings,
            mode=selected_mode,
            field_discovery_service=None,
        )
        return ProcessContract(
            project_root=project_root,
            pipelines=pipelines,
            graph_factory=LangGraphWorkflowFactory(),
        )
    resolved_service = field_discovery_service
    close_discovery_service: Callable[[], Awaitable[None]] | None = None
    if resolved_service is None:
        embedding_client = await _build_field_discovery_embedding_client(
            project_root=project_root, settings=settings
        )
        resolved_service = StructuredFieldDiscoveryService(
            project_root=project_root,
            settings=settings,
            embedding_client=embedding_client,
        )
        close_discovery_service = resolved_service.close
    pipelines = await _build_pipelines(
        project_root=project_root,
        settings=settings,
        mode=selected_mode,
        field_discovery_service=resolved_service,
    )
    return DiscoverContractFields(
        project_root=project_root,
        pipelines=pipelines,
        graph_factory=LangGraphWorkflowFactory(),
        close_discovery_service=close_discovery_service,
    )


async def _build_field_discovery_embedding_client(
    *, project_root: Path, settings: ProjectSettings
) -> Qwen3VLEmbeddingClient:
    """按统一向量策略组装字段发现批次的共享 Embedding 客户端。"""

    # settings 加载不承诺写入进程环境；显式加载后再读取 API key。
    await run_blocking(load_dotenv, project_root / ".env")
    embedding = settings.models.embedding
    policy = await load_contract_embedding_policy(
        _resolve_project_path(project_root, settings.paths.contract_embedding_policy)
    )
    return Qwen3VLEmbeddingClient(
        base_url=embedding.base_url,
        api_key=os.getenv(embedding.api_key_env) or "",
        model=embedding.model,
        endpoint=embedding.endpoint,
        timeout_seconds=embedding.timeout_seconds,
        dimensions=embedding.dimensions,
        max_concurrent_requests=embedding.max_concurrent_requests,
        normalize=embedding.normalize,
        policy=policy,
    )


async def _build_pipelines(
    *,
    project_root: Path,
    settings: ProjectSettings,
    mode: RuntimeMode,
    field_discovery_service: FieldDiscoveryService | None,
) -> ValidatedExtractionPipelines:
    """在昂贵资源初始化前冻结目录快照并选择 Core 策略。"""

    catalog = YamlFieldCatalog(
        core_path=project_root
        / (
            settings.paths.discovery_core_fields
            if mode is RuntimeMode.DISCOVERY
            else settings.paths.core_fields
        ),
        attribute_path=project_root
        / (
            settings.paths.discovery_attribute_fields
            if mode is RuntimeMode.DISCOVERY
            else settings.paths.attribute_fields
        ),
    )
    core_snapshot = await catalog.snapshot(FieldKind.CORE)
    attribute_snapshot = await catalog.snapshot(FieldKind.ATTRIBUTE)
    validate_core_catalog_for_mode(mode, core_snapshot)
    core_service = (
        EmptyCoreExtractionService(core_snapshot.schema_version)
        if core_snapshot.is_empty
        else CoreExtractionService()
    )
    return ValidatedExtractionPipelines(
        project_root,
        settings,
        runtime_mode=mode,
        core_catalog_snapshot=core_snapshot,
        attribute_catalog_snapshot=attribute_snapshot,
        field_catalog=catalog,
        field_discovery_service=field_discovery_service,
        core_service=core_service,
    )
