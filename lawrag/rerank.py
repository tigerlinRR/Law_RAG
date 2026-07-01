"""Cross-encoder reranker client -> local vLLM /rerank endpoint.

A reranker re-scores each (query, passage) pair jointly, which is far more precise
than the first-stage vector/keyword scores. We use it to reorder the fused
candidate pool and keep only the best ones.
"""
from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CONFIG


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
def _rerank_call(query: str, documents: list[str]) -> list[float]:
    resp = httpx.post(
        f"{CONFIG.rerank_base_url}/rerank",
        json={"model": CONFIG.rerank_model, "query": query, "documents": documents},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # vLLM returns {"results": [{"index": i, "relevance_score": s}, ...]}.
    scores = [0.0] * len(documents)
    for r in data["results"]:
        scores[r["index"]] = r["relevance_score"]
    return scores


def rerank(query: str, documents: list[str]) -> list[float]:
    """Return a relevance score per document (same order as input)."""
    if not documents:
        return []
    return _rerank_call(query, documents)
