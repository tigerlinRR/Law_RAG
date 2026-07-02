"""Hybrid retrieval: semantic (vector) + keyword (full-text), fused with RRF.

Reciprocal Rank Fusion (RRF) needs no score normalization and is robust across
very different scorers — the standard, low-maintenance choice for hybrid search.

Metadata filters (client / matter / doc_type / author) are applied in SQL. The
client/matter filter is also the hook for future access control (ethical walls):
a caller's permitted scope simply becomes a mandatory filter.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import db, embed, rerank as _rerank
from .config import CONFIG

_RRF_K = 60


@dataclass
class Filters:
    client: str | None = None
    matter: str | None = None
    doc_type: str | None = None
    author: str | None = None

    def where(self) -> tuple[str, list]:
        clauses, params = [], []
        for col in ("client", "matter", "doc_type", "author"):
            val = getattr(self, col)
            if val:
                clauses.append(f"d.{col} = %s")
                params.append(val)
        sql = (" AND " + " AND ".join(clauses)) if clauses else ""
        return sql, params


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    score: float                 # final ranking score (rerank score if reranked, else RRF)
    content: str
    page: int | None
    filename: str
    doc_type: str | None
    client: str | None
    matter: str | None
    author: str | None
    doc_date: str | None
    rrf_score: float = 0.0       # first-stage fused score (kept for transparency)
    reranked: bool = False

    def citation(self) -> str:
        loc = f" p.{self.page}" if self.page else ""
        tag = f" [{self.doc_type}]" if self.doc_type else ""
        return f"{self.filename}{loc}{tag}"


def search(
    query: str,
    filters: Filters | None = None,
    top_k: int | None = None,
    use_rerank: bool | None = None,
    allowed_clients: list[str] | None = None,
    meta_filters: dict[str, str] | None = None,
    exclude_document_ids: list[int] | None = None,
) -> list[Hit]:
    """`allowed_clients`: None = unrestricted (admin); a list = hard limit to those
    clients (ethical wall). An empty list means the caller may see nothing.

    `meta_filters`: containment filters against list-valued documents.meta fields,
    e.g. {"filing_items": "1.01"} matches any document whose meta.filing_items
    array includes "1.01" — a single 8-K commonly reports several Items at once,
    so this is containment (JSONB @>), not exact equality.

    `exclude_document_ids`: hide specific documents from results — e.g. for a
    held-out precedent-quality eval, exclude the real filing that resulted from
    the very contract being drafted from, so it can't leak into its own "precedent"."""
    filters = filters or Filters()
    top_k = top_k or CONFIG.topk_final
    use_rerank = CONFIG.rerank_enabled if use_rerank is None else use_rerank
    fwhere, fparams = filters.where()
    for key, val in (meta_filters or {}).items():
        fwhere += " AND d.meta @> %s::jsonb"
        fparams.append(json.dumps({key: [val]}))
    if exclude_document_ids:
        fwhere += " AND NOT (d.id = ANY(%s))"
        fparams.append(exclude_document_ids)
    # Mandatory access-control filter, applied on top of any user-chosen filters.
    if allowed_clients is not None:
        if not allowed_clients:
            return []
        fwhere += " AND d.client = ANY(%s)"
        fparams = [*fparams, allowed_clients]
    qvec = embed.embed_query(query)

    vector_sql = f"""
        SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> %s::vector) AS rank
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE TRUE {fwhere}
        ORDER BY c.embedding <=> %s::vector
        LIMIT {CONFIG.topk_vector}
    """
    text_sql = f"""
        SELECT c.id, ROW_NUMBER() OVER (
                   ORDER BY ts_rank(c.tsv, plainto_tsquery('english', %s)) DESC) AS rank
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.tsv @@ plainto_tsquery('english', %s) {fwhere}
        ORDER BY ts_rank(c.tsv, plainto_tsquery('english', %s)) DESC
        LIMIT {CONFIG.topk_text}
    """

    scores: dict[int, float] = {}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(vector_sql, [qvec, *fparams, qvec])
            for cid, rank in cur.fetchall():
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
            cur.execute(text_sql, [query, query, *fparams, query])
            for cid, rank in cur.fetchall():
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank)

        if not scores:
            return []

        # First stage: keep a candidate POOL (larger than top_k) for the reranker
        # to work on. Without rerank the pool is just trimmed to top_k directly.
        pool_size = max(top_k, CONFIG.rerank_candidates if use_rerank else top_k)
        cand_ids = sorted(scores, key=scores.get, reverse=True)[:pool_size]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.document_id, c.content, c.page,
                       d.filename, d.doc_type, d.client, d.matter, d.author,
                       to_char(d.doc_date, 'YYYY-MM-DD')
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id = ANY(%s)
                """,
                (cand_ids,),
            )
            rows = {r[0]: r for r in cur.fetchall()}

    def make_hit(cid: int, final_score: float, reranked: bool) -> Hit:
        r = rows[cid]
        return Hit(
            chunk_id=r[0], document_id=r[1], score=final_score, content=r[2],
            page=r[3], filename=r[4], doc_type=r[5], client=r[6], matter=r[7],
            author=r[8], doc_date=r[9], rrf_score=scores[cid], reranked=reranked,
        )

    # Second stage: cross-encoder rerank of the candidate pool.
    if use_rerank and len(cand_ids) > 1:
        try:
            rr = _rerank.rerank(query, [rows[cid][2] for cid in cand_ids])
            order = sorted(range(len(cand_ids)), key=lambda i: rr[i], reverse=True)
            return [make_hit(cand_ids[i], rr[i], True) for i in order[:top_k]]
        except Exception:  # noqa: BLE001 — reranker down: fall back to RRF order
            pass

    return [make_hit(cid, scores[cid], False) for cid in cand_ids[:top_k]]
