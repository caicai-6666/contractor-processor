"""Core、Clause 与摘要共用的完整多模态 Prompt 前缀。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from contract_processor.async_utils import run_blocking


SYSTEM_MESSAGE = "你必须以合同 PDF 页面图像为准，并严格遵守当前 JSON Schema。"
PROMPT_TEMPLATE_ROOT = Path(__file__).parent / "templates"
COMMON_PROMPT_PATH = PROMPT_TEMPLATE_ROOT / "00_contract_pdf_common_prefix.txt"
REQUIRED_EXTRACTION_PROMPTS = (
    "core/prompts/01_understand_contract.txt",
    "core/prompts/02_extract_core.txt",
    "clause/prompts/01_discover_clause_structure.txt",
    "clause/prompts/01b_consolidate_clause_boundaries.txt",
    "clause/prompts/02_review_clause_candidates.txt",
    "clause/prompts/03_extract_clause_unit.txt",
    "abstract/prompts/01_extract_summary_sections.txt",
    "abstract/prompts/02_retry_summary_section.txt",
    "attribute/prompts/01_extract_attribute_field.txt",
)


def build_page_visibility_context(page_count: int) -> str:
    """构造三条线路完全一致、由程序保证为真的页面范围说明。"""

    if page_count < 1:
        raise ValueError("page_count 必须大于 0。")
    rendered_pages = "、".join(str(page) for page in range(1, page_count + 1))
    return (
        "【页面可见范围（程序提供，优先级最高）】\n"
        f"- 原始 PDF 共 {page_count} 个物理页，本次提供全部页面：{rendered_pages}。\n"
        "- 只能依据这些页面图像；不得根据合同常见结构、文件名或外部知识补写。\n"
        "- 所有页码字段使用物理 PDF 页码，不使用或推断合同内部印刷页码。\n"
        "- 同一内容跨页时，应保留正文实际出现的全部物理页。"
    )


async def build_common_prefix(page_count: int) -> str:
    """公共规则、页面范围与图像共同组成稳定前缀，任务说明只能放在图像后。"""

    common_rules = await build_common_rules()
    return "\n\n".join([common_rules, build_page_visibility_context(page_count)])


async def build_common_rules() -> str:
    """只读取跨合同不变的 PDF 公共规则，供批处理实验构造更长的静态前缀。"""

    return await run_blocking(_read_common_rules_sync)


def _read_common_rules_sync() -> str:
    """供受控工作线程读取稳定公共规则，不混入每份合同的页数信息。"""

    return COMMON_PROMPT_PATH.read_text(encoding="utf-8").strip()


def _build_common_prefix_sync(page_count: int) -> str:
    """供工作线程调用的公共 Prompt 文件读取实现。"""

    common_rules = _read_common_rules_sync()
    return "\n\n".join([common_rules, build_page_visibility_context(page_count)])


async def compute_prompt_version(project_root: Path) -> str:
    """以全部正式模型 Prompt 的路径和字节计算可复现版本。"""

    return await run_blocking(_compute_prompt_version_sync, project_root)


def _compute_prompt_version_sync(project_root: Path) -> str:
    """供工作线程调用的 Prompt 哈希实现。"""

    source_package_root = project_root / "src/contract_processor"
    installed_package_root = Path(__file__).resolve().parents[2]
    package_root = (
        source_package_root if source_package_root.is_dir() else installed_package_root
    )
    common_prompt_path = (
        package_root
        / "application/prompts/templates/00_contract_pdf_common_prefix.txt"
    )
    extraction_root = package_root / "infrastructure/extraction"
    prompt_paths = [common_prompt_path]
    prompt_paths.extend(
        extraction_root / relative_path
        for relative_path in REQUIRED_EXTRACTION_PROMPTS
    )
    if any(not path.is_file() for path in prompt_paths):
        raise FileNotFoundError("正式抽取 Prompt 不完整，无法计算 prompt_version。")
    digest = sha256()
    for path in prompt_paths:
        # 只哈希包内相对路径，源码运行和 wheel 安装得到相同版本。
        stable_path = path.relative_to(package_root).as_posix()
        digest.update(stable_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
