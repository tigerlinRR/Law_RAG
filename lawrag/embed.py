"""Embedding client -> local vLLM (OpenAI-compatible) endpoint.

Qwen3-Embedding is asymmetric: queries should carry a task instruction, documents
should not. We follow the model's recommended format for best retrieval quality.
"""
from __future__ import annotations

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CONFIG

_client = OpenAI(base_url=CONFIG.embed_base_url, api_key="not-needed-local")

# Task instruction prepended to queries only (Qwen3-Embedding convention).
_QUERY_INSTRUCT = (
    "Instruct: Given a legal document search query, "
    "retrieve the most relevant document passages.\nQuery: "
)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
def _embed(inputs: list[str]) -> list[list[float]]:
    resp = _client.embeddings.create(model=CONFIG.embed_model, input=inputs)
    return [d.embedding for d in resp.data]


def embed_documents(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        out.extend(_embed(texts[i : i + batch_size]))
    return out


def embed_query(query: str) -> list[float]:
    return _embed([_QUERY_INSTRUCT + query])[0]
