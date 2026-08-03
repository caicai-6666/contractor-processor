"""兼容导出；入库实验统一复用正式 Embedding 实现。"""

from contract_processor.infrastructure.embedding.qwen3_vl import (
    ContractEmbeddingPolicy,
    Qwen3VLEmbeddingClient,
    fuse_page_embeddings,
    load_contract_embedding_policy,
    normalize_vector,
)

VISUAL_STRATEGY = "normalized_page_mean_v1"

__all__ = [
    "ContractEmbeddingPolicy",
    "Qwen3VLEmbeddingClient",
    "VISUAL_STRATEGY",
    "fuse_page_embeddings",
    "load_contract_embedding_policy",
    "normalize_vector",
]
