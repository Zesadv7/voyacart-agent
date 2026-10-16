"""兼容既有评测导入；嵌入实现由运行时与评测共用。"""
from app.infrastructure.embedding.local_multilingual import (
    ENCODING_VERSION, MODEL, LocalMultilingualEmbedding, model_files, model_version,
)

__all__ = ["ENCODING_VERSION", "MODEL", "LocalMultilingualEmbedding", "model_files", "model_version"]
