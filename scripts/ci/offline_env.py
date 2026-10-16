"""只允许本机假服务的环境；不读取或打印宿主凭据。"""
from __future__ import annotations

import os
from pathlib import Path


def isolated_env(data_dir: Path, *, mock_url: str = "http://127.0.0.1:1/v1") -> dict[str, str]:
    # 避免继承真实服务地址、认证、代理、追踪或用户 .env。
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    env.update({
        "HOME": str(data_dir / "home"), "PYTHONUNBUFFERED": "1", "PYTHON_DOTENV_DISABLED": "1",
        "DATA_DIR": str(data_dir), "DATABASE_URL": f"sqlite+aiosqlite:///{data_dir / 'voyacart.db'}",
        "LLM_BASE_URL": mock_url, "LLM_API_KEY": "ci-local-placeholder", "LLM_MODEL": "mock-chat",
        "LLM_FALLBACK_MODEL": "", "LLM_MIN_INTERVAL_SECONDS": "0", "LLM_MAX_RETRIES": "0",
        "EMBEDDING_BASE_URL": mock_url, "EMBEDDING_API_KEY": "ci-local-placeholder",
        "EMBEDDING_MODEL": "mock-embedding", "EMBEDDING_DIM": "1024", "EMBEDDING_MAX_BATCH": "100",
        "QDRANT_URL": "", "RERANKER_BASE_URL": "", "RERANKER_MODEL": "", "RERANKER_MODE": "disabled",
        "REDIS_URL": "", "QUEUE_ENABLED": "0", "AGUI_QUEUE_ENABLED": "0", "SEMANTIC_CACHE_ENABLED": "0",
        "LOCAL_EMBEDDING_FALLBACK": "0", "HYBRID_RECALL_ENABLED": "0", "AUTOMATION_WORKER_MODE": "off",
        "TAVILY_API_KEY": "", "OTEL_SDK_DISABLED": "true", "LANGFUSE_SECRET_KEY": "", "LANGFUSE_PUBLIC_KEY": "",
        "IDENTITY_MODE": "demo", "MOCK_TTFT_SECONDS": "0", "MOCK_TOKEN_SECONDS": "0",
        "MOCK_ORDER_RATE": "0", "MOCK_RESPONSE_TOKENS": "80", "MOCK_FAULT_CONTROL": "1",
    })
    return env
