"""合同领域使用的稳定枚举。"""

from enum import Enum


class RuntimeMode(str, Enum):
    """项目允许构建的两种互斥运行模式。"""

    DISCOVERY = "discovery"
    PRODUCTION = "production"


class FieldKind(str, Enum):
    """字段所属的字段库。"""

    CORE = "core"
    ATTRIBUTE = "attribute"


class ExtractionStatus(str, Enum):
    """字段在当前合同中的证据状态。"""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    NOT_APPLICABLE = "not_applicable"


class ReviewStatus(str, Enum):
    """Attribute 的专家审核状态。"""

    PENDING = "pending"
    APPROVED_CORE = "approved_core"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class MergeAction(str, Enum):
    """候选字段经归并后可执行的操作。"""

    REUSE = "reuse"
    ENRICH = "enrich"
    CREATE = "create"
    DISCARD = "discard"
