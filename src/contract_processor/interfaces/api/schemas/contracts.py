"""合同处理与终审确认的 FastAPI DTO。"""

from pydantic import BaseModel, ConfigDict, Field

from contract_processor.application.schemas.contract_ingestion import (
    ContractReviewConfirmation,
    ContractReviewTrace,
)
from contract_processor.application.schemas.contract_processing import (
    ContractProcessingResult,
)


class ApiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractProcessAccepted(ApiSchema):
    """未来上传接口异步受理后的响应。"""

    job_id: str = Field(min_length=1)
    status: str = Field(default="pending", pattern="^(pending|running)$")


class ContractProcessResponse(ContractProcessingResult):
    """前端终审页读取的完整自动化候选。"""


__all__ = [
    "ContractProcessAccepted",
    "ContractProcessResponse",
    "ContractReviewConfirmation",
    "ContractReviewTrace",
]
