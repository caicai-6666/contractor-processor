"""Qwen3-VL 文本/页面向量客户端与多页合同向量融合。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import fitz
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from contract_processor.async_utils import run_blocking


VECTOR_TEXT_FIELDS = (
    "contract_name_vector",
    "product_names_vector",
    "abstract_vector",
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
FIELD_SUMMARY_INSTRUCTION = (
    "Represent this metadata field definition for semantic similarity retrieval."
)


class ContractEmbeddingPolicy(BaseModel):
    """版本化向量指令和视觉融合配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1)
    text_instructions: dict[str, str]
    visual_instruction: str = Field(min_length=1)
    visual_strategy: str = Field(pattern="^normalized_page_mean_v1$")
    render_scale: float = Field(gt=0)
    render_format: str = Field(pattern="^jpeg$")
    jpeg_quality: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def validate_text_fields(self) -> "ContractEmbeddingPolicy":
        expected = set(VECTOR_TEXT_FIELDS)
        actual = set(self.text_instructions)
        if actual != expected:
            raise ValueError(
                "text_instructions 字段必须恰好为 "
                f"{sorted(expected)}；实际为 {sorted(actual)}。"
            )
        if any(not value.strip() for value in self.text_instructions.values()):
            raise ValueError("text_instructions 不允许空指令。")
        return self

    @property
    def instruction_version(self) -> str:
        """用实际策略内容哈希标识向量空间，避免只改文本而忘记升版本。"""

        payload = self.model_dump(mode="json")
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


async def load_contract_embedding_policy(path: Path) -> ContractEmbeddingPolicy:
    def load_sync() -> ContractEmbeddingPolicy:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ContractEmbeddingPolicy.model_validate(payload)

    return await run_blocking(load_sync)


def normalize_vector(vector: Sequence[float]) -> list[float]:
    """以 float64 累加范数并拒绝空、零或非有限向量。"""

    if not vector:
        raise ValueError("Embedding 服务返回空向量。")
    if any(not math.isfinite(float(value)) for value in vector):
        raise ValueError("Embedding 服务返回非有限数值。")
    norm = math.sqrt(math.fsum(float(value) * float(value) for value in vector))
    if norm <= 0:
        raise ValueError("Embedding 服务返回零向量。")
    return [float(value) / norm for value in vector]


def fuse_page_embeddings(page_vectors: Sequence[Sequence[float]]) -> list[float]:
    """逐页归一化、等权平均后再次归一化。"""

    if not page_vectors:
        raise ValueError("PDF 没有可融合的页面向量。")
    normalized = [normalize_vector(vector) for vector in page_vectors]
    dimensions = len(normalized[0])
    if any(len(vector) != dimensions for vector in normalized):
        raise ValueError("页面向量维度不一致。")
    mean = [
        math.fsum(vector[index] for vector in normalized) / len(normalized)
        for index in range(dimensions)
    ]
    return normalize_vector(mean)


def _render_pdf_pages_sync(
    pdf_path: Path,
    *,
    scale: float,
    jpeg_quality: int,
) -> list[bytes]:
    """在同一阻塞线程顺序访问 PyMuPDF，避免跨线程共享 Document。"""

    document = fitz.open(pdf_path)
    try:
        if document.page_count < 1:
            raise ValueError("PDF 不包含可渲染页面。")
        pages: list[bytes] = []
        for index in range(document.page_count):
            pixmap = document.load_page(index).get_pixmap(
                matrix=fitz.Matrix(scale, scale), alpha=False
            )
            pages.append(pixmap.tobytes("jpeg", jpg_quality=jpeg_quality))
        return pages
    finally:
        document.close()


class Qwen3VLEmbeddingClient:
    """通过 vLLM Chat Embeddings 访问统一文本/图像向量空间。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        endpoint: str,
        timeout_seconds: float,
        dimensions: int,
        max_concurrent_requests: int,
        normalize: bool,
        policy: ContractEmbeddingPolicy,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            trust_env=False,
        )
        self._model = model
        self._endpoint = "/" + endpoint.strip("/")
        self._dimensions = dimensions
        self._normalize = normalize
        self._policy = policy
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str:
        return self._model

    @property
    def instruction_version(self) -> str:
        return self._policy.instruction_version

    @property
    def field_summary_instruction_version(self) -> str:
        """标识字段发现专用文本指令，避免与合同入库向量策略版本混淆。"""

        return hashlib.sha256(FIELD_SUMMARY_INSTRUCTION.encode("utf-8")).hexdigest()

    @property
    def visual_strategy(self) -> str:
        return self._policy.visual_strategy

    async def probe(self) -> None:
        response = await self._client.get("/models")
        response.raise_for_status()
        model_ids = {
            item.get("id")
            for item in response.json().get("data", [])
            if isinstance(item, dict)
        }
        if self._model not in model_ids:
            raise RuntimeError(
                f"Embedding 服务未加载配置模型 {self._model}；"
                f"实际模型：{sorted(model_ids)}"
            )

    @staticmethod
    def _messages(
        content: list[dict[str, Any]], *, instruction: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": [{"type": "text", "text": instruction}],
            },
            {"role": "user", "content": content},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
            },
        ]

    async def _post_embedding(self, messages: list[dict[str, Any]]) -> list[float]:
        payload = {
            "model": self._model,
            "messages": messages,
            "encoding_format": "float",
            "continue_final_message": True,
            "add_special_tokens": True,
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with self._semaphore:
                    response = await self._client.post(self._endpoint, json=payload)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                response.raise_for_status()
                embedding = response.json()["data"][0]["embedding"]
                if not isinstance(embedding, list):
                    raise TypeError("Embedding 响应 data[0].embedding 不是数组。")
                vector = [float(value) for value in embedding]
                if len(vector) != self._dimensions:
                    raise ValueError(
                        f"Embedding 维度为 {len(vector)}，配置要求 {self._dimensions}。"
                    )
                return normalize_vector(vector) if self._normalize else vector
            except httpx.TransportError as error:
                last_error = error
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise
            except httpx.HTTPStatusError:
                # 非暂态 HTTP 错误不重试；暂态错误已在上方按状态码完成一次重试。
                raise
        raise RuntimeError("Embedding 请求失败。") from last_error

    async def embed_text_fields(
        self, inputs: Mapping[str, str]
    ) -> dict[str, list[float]]:
        unknown = set(inputs) - set(self._policy.text_instructions)
        if unknown:
            raise ValueError(f"不支持的文本向量字段：{sorted(unknown)}")

        async def embed_one(field: str, text: str) -> tuple[str, list[float]]:
            messages = self._messages(
                [{"type": "text", "text": text}],
                instruction=self._policy.text_instructions[field],
            )
            return field, await self._post_embedding(messages)

        pairs = await asyncio.gather(
            *(embed_one(field, text) for field, text in inputs.items())
        )
        return dict(pairs)

    async def embed_field_summary(self, summary: str) -> list[float]:
        """为 discovery 批次内字段相似度生成临时向量。"""

        if not summary.strip():
            raise ValueError("字段摘要不能为空。")
        messages = self._messages(
            [{"type": "text", "text": summary.strip()}],
            instruction=FIELD_SUMMARY_INSTRUCTION,
        )
        return await self._post_embedding(messages)

    async def embed_pdf(self, pdf_path: Path) -> tuple[list[float], int]:
        pages = await run_blocking(
            _render_pdf_pages_sync,
            pdf_path,
            scale=self._policy.render_scale,
            jpeg_quality=self._policy.jpeg_quality,
        )

        async def embed_page(image_bytes: bytes) -> list[float]:
            data_url = "data:image/jpeg;base64," + base64.b64encode(
                image_bytes
            ).decode("ascii")
            messages = self._messages(
                [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": ""},
                ],
                instruction=self._policy.visual_instruction,
            )
            return await self._post_embedding(messages)

        page_vectors = await asyncio.gather(*(embed_page(page) for page in pages))
        return fuse_page_embeddings(page_vectors), len(page_vectors)

    async def close(self) -> None:
        await self._client.aclose()
