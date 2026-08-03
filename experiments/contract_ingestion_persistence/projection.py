"""兼容导出；投影规则的唯一实现位于正式应用层。"""

from contract_processor.application.services.contract_ingestion_projection import (
    OWN_COMPANY_NAMES_ENV,
    build_contract_search_projection,
    is_own_company_name,
    parse_own_company_names,
)

build_search_projection = build_contract_search_projection

__all__ = [
    "OWN_COMPANY_NAMES_ENV",
    "build_search_projection",
    "is_own_company_name",
    "parse_own_company_names",
]
