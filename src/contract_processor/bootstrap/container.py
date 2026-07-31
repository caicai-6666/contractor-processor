"""本地 CLI 使用的依赖组装入口。"""

from pathlib import Path

from contract_processor.application.use_cases.inspect_field_catalog import InspectFieldCatalog
from contract_processor.infrastructure.persistence.yaml_field_catalog import YamlFieldCatalog


def build_inspect_field_catalog(project_root: Path) -> InspectFieldCatalog:
    """组装不依赖模型服务的字段库检查用例。"""

    catalog = YamlFieldCatalog(
        core_path=project_root / "description/fields/core/core.yaml",
        attribute_path=project_root / "description/fields/attribute/attribute.yaml",
    )
    return InspectFieldCatalog(catalog)
