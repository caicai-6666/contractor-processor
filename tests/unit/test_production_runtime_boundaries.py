"""正式运行时的异步与无实验副作用架构门禁。"""

import ast
import asyncio
import inspect
import time
from pathlib import Path

from contract_processor.application.prompts.pdf_prefix import (
    build_common_prefix,
    compute_prompt_version,
)
from contract_processor.application.use_cases.discover_contract_fields import (
    DiscoverContractFields,
)
from contract_processor.application.use_cases.process_contract import ProcessContract
from contract_processor.application.use_cases.ingest_reviewed_contract import (
    IngestReviewedContract,
)
from contract_processor.async_utils import run_blocking
from contract_processor.infrastructure.extraction.abstract.pipeline import (
    run_abstract_extraction,
)
from contract_processor.infrastructure.extraction.attribute.pipeline import (
    run_attribute_extraction,
)
from contract_processor.infrastructure.extraction.clause.pipeline import (
    run_clause_extraction,
)
from contract_processor.infrastructure.extraction.core import EmptyCoreExtractionService
from contract_processor.infrastructure.extraction.core.pipeline import run_core_extraction
from contract_processor.infrastructure.llm.openai_vllm import OpenAIVllmVisionClient
from contract_processor.infrastructure.pdf.document_identity import compute_document_id
from contract_processor.infrastructure.persistence.elasticsearch_contract_index import (
    ElasticsearchContractIndexRepository,
    ElasticsearchMappingFactory,
)
from contract_processor.infrastructure.persistence.local_source_document_store import (
    LocalSourceDocumentStore,
)
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog
from contract_processor.interfaces.cli.common import (
    load_cli_settings,
    resolve_from_root,
    resolve_project_root,
)
from contract_processor.settings import load_project_settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORMAL_EXTRACTION_ROOT = PROJECT_ROOT / "src/contract_processor/infrastructure/extraction"
BLOCKING_IO_CALLS = {
    "flush",
    "glob",
    "is_dir",
    "is_file",
    "load_dotenv",
    "mkdir",
    "open",
    "read_bytes",
    "read_text",
    "resolve",
    "rglob",
    "safe_load",
    "write",
    "write_bytes",
    "write_text",
}


def test_all_external_production_boundaries_are_async() -> None:
    assert inspect.iscoroutinefunction(ProcessContract.execute)
    assert inspect.iscoroutinefunction(DiscoverContractFields.execute)
    assert inspect.iscoroutinefunction(EmptyCoreExtractionService.extract)
    assert inspect.iscoroutinefunction(run_core_extraction)
    assert inspect.iscoroutinefunction(run_clause_extraction)
    assert inspect.iscoroutinefunction(run_abstract_extraction)
    assert inspect.iscoroutinefunction(run_attribute_extraction)
    assert inspect.iscoroutinefunction(IngestReviewedContract.execute)
    assert inspect.iscoroutinefunction(ElasticsearchContractIndexRepository.save)
    assert inspect.iscoroutinefunction(ElasticsearchContractIndexRepository.get)
    assert inspect.iscoroutinefunction(LocalSourceDocumentStore.save)
    assert inspect.iscoroutinefunction(LocalSourceDocumentStore.resolve)
    assert inspect.iscoroutinefunction(ElasticsearchMappingFactory.build)
    assert inspect.iscoroutinefunction(YamlFieldCatalog.load)
    assert inspect.iscoroutinefunction(YamlFieldCatalog.snapshot)
    assert inspect.iscoroutinefunction(load_project_settings)
    assert inspect.iscoroutinefunction(build_common_prefix)
    assert inspect.iscoroutinefunction(compute_prompt_version)
    assert inspect.iscoroutinefunction(compute_document_id)
    assert inspect.iscoroutinefunction(resolve_project_root)
    assert inspect.iscoroutinefunction(load_cli_settings)
    assert inspect.iscoroutinefunction(resolve_from_root)


def test_formal_extraction_code_has_no_experiment_file_or_console_side_effects() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in FORMAL_EXTRACTION_ROOT.rglob("*.py")
    )

    assert "print(" not in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    assert "data/runs" not in source
    assert "raw_response_path" not in source


def test_no_formal_source_uses_debug_print() -> None:
    source_root = PROJECT_ROOT / "src/contract_processor"
    source_paths = list(source_root.rglob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    non_storage_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in source_paths
        if path.name != "local_source_document_store.py"
    )

    assert "print(" not in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    # 正式 PDF 落盘只允许出现在明确的 SourceDocumentStore 适配器中。
    assert ".mkdir(" not in non_storage_source
    assert "data/runs" not in source


def test_formal_runtime_has_no_synchronous_network_clients_or_graph_invocation() -> None:
    source_root = PROJECT_ROOT / "src/contract_processor"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")
    )

    assert "httpx.Client(" not in source
    assert "from openai import OpenAI" not in source
    assert ".invoke({" not in source


def test_blocking_file_adapter_does_not_block_event_loop() -> None:
    async def probe() -> None:
        blocking = asyncio.create_task(run_blocking(time.sleep, 0.1))
        heartbeat = asyncio.create_task(asyncio.sleep(0.001))
        completed, _ = await asyncio.wait(
            {blocking, heartbeat}, return_when=asyncio.FIRST_COMPLETED
        )

        assert heartbeat in completed
        assert blocking not in completed
        await blocking

    asyncio.run(probe())


def test_local_vllm_client_does_not_inherit_system_proxy() -> None:
    client = OpenAIVllmVisionClient(
        base_url="http://127.0.0.1:8000/v1",
        api_key="test",
        model="test",
        timeout_seconds=1,
    )

    assert client._http_client._trust_env is False
    asyncio.run(client.close())


def test_async_functions_do_not_call_blocking_io_directly() -> None:
    violations: list[str] = []

    class AsyncIoVisitor(ast.NodeVisitor):
        def __init__(self, path: Path) -> None:
            self.path = path
            self.async_functions: list[str] = []
            self.run_blocking_depth = 0

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.async_functions.append(node.name)
            for child in node.body:
                self.visit(child)
            self.async_functions.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if not self.async_functions:
                for child in node.body:
                    self.visit(child)

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )
            if (
                self.async_functions
                and name in BLOCKING_IO_CALLS
                and self.run_blocking_depth == 0
            ):
                violations.append(
                    f"{self.path}:{node.lineno}: "
                    f"{self.async_functions[-1]} 直接调用 {name}"
                )
            enters_adapter = name == "run_blocking"
            if enters_adapter:
                self.run_blocking_depth += 1
            self.generic_visit(node)
            if enters_adapter:
                self.run_blocking_depth -= 1

    source_root = PROJECT_ROOT / "src/contract_processor"
    for path in source_root.rglob("*.py"):
        AsyncIoVisitor(path).visit(ast.parse(path.read_text(encoding="utf-8")))

    assert violations == []


def test_public_synchronous_functions_are_io_free() -> None:
    violations: list[str] = []
    source_root = PROJECT_ROOT / "src/contract_processor"

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        candidates: list[ast.FunctionDef] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                candidates.append(node)
            elif isinstance(node, ast.ClassDef):
                candidates.extend(
                    child
                    for child in node.body
                    if isinstance(child, ast.FunctionDef)
                    and not child.name.startswith("_")
                )
        for function in candidates:
            for call in (
                child for child in ast.walk(function) if isinstance(child, ast.Call)
            ):
                called = call.func
                name = (
                    called.attr
                    if isinstance(called, ast.Attribute)
                    else called.id
                    if isinstance(called, ast.Name)
                    else ""
                )
                if name in BLOCKING_IO_CALLS:
                    violations.append(
                        f"{path}:{call.lineno}: {function.name} 调用 {name}"
                    )

    assert violations == []


def test_modules_using_asyncio_import_asyncio() -> None:
    """防止异步重构后仅在实际执行路径暴露缺失导入。"""

    source_root = PROJECT_ROOT / "src/contract_processor"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses_asyncio = any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "asyncio"
            for node in ast.walk(tree)
        )
        imports_asyncio = any(
            isinstance(node, ast.Import)
            and any(alias.name == "asyncio" for alias in node.names)
            for node in tree.body
        )
        if uses_asyncio:
            assert imports_asyncio, f"{path} 使用 asyncio 但未导入 asyncio"
