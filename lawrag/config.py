"""Central configuration, loaded from .env (see project root)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of CWD.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Config:
    # Postgres
    pg_host: str = os.getenv("PG_HOST", "localhost")
    pg_port: int = _int("PG_PORT", 5434)
    pg_db: str = os.getenv("PG_DB", "lawrag")
    pg_user: str = os.getenv("PG_USER", "lawrag")
    pg_password: str = os.getenv("PG_PASSWORD", "lawrag")

    # Embedding service (vLLM, OpenAI-compatible)
    embed_base_url: str = os.getenv("EMBED_BASE_URL", "http://localhost:8010/v1")
    embed_model: str = os.getenv("EMBED_MODEL", "qwen3-embed")
    embed_dim: int = _int("EMBED_DIM", 1024)

    # Chunking
    chunk_chars: int = _int("CHUNK_CHARS", 1200)
    chunk_overlap: int = _int("CHUNK_OVERLAP", 150)

    # LLM (vLLM Qwen3.6-35B) — due-diligence summarization / extraction
    llm_base_url: str = os.getenv("LLM_BASE_URL", "http://localhost:8012/v1")
    llm_model: str = os.getenv("LLM_MODEL", "qwen3.6")
    # Rough char budget for a single LLM pass (~4 chars/token, 32k ctx, leave room).
    llm_max_ctx_chars: int = _int("LLM_MAX_CTX_CHARS", 90000)

    # Reranker (vLLM cross-encoder)
    rerank_base_url: str = os.getenv("RERANK_BASE_URL", "http://localhost:8011")
    rerank_model: str = os.getenv("RERANK_MODEL", "qwen3-rerank")
    rerank_enabled: bool = os.getenv("RERANK_ENABLED", "1") == "1"

    # Retrieval
    topk_vector: int = _int("TOPK_VECTOR", 20)
    topk_text: int = _int("TOPK_TEXT", 20)
    rerank_candidates: int = _int("RERANK_CANDIDATES", 30)
    topk_final: int = _int("TOPK_FINAL", 8)

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user} password={self.pg_password}"
        )


CONFIG = Config()
ROOT = _ROOT
