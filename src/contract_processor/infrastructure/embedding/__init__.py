"""向量模型基础设施适配器。"""

from .qwen3_vl import (
    ContractEmbeddingPolicy,
    Qwen3VLEmbeddingClient,
    fuse_page_embeddings,
    load_contract_embedding_policy,
    normalize_vector,
)

__all__ = [
    "ContractEmbeddingPolicy",
    "Qwen3VLEmbeddingClient",
    "fuse_page_embeddings",
    "load_contract_embedding_policy",
    "normalize_vector",
]
