#!/usr/bin/env python
"""Query the knowledge base with hybrid search; prints ranked hits + citations.

Usage:
  python scripts/query.py "employee stock incentive plan" [--client Richtech]
                          [--doc-type S-8] [--author "Jane Doe"] [-k 8]
"""
from __future__ import annotations

import argparse

from rich.console import Console
from rich.panel import Panel

from lawrag.retrieve import Filters, search

console = Console()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--client")
    ap.add_argument("--matter")
    ap.add_argument("--doc-type", dest="doc_type")
    ap.add_argument("--author")
    ap.add_argument("-k", type=int, default=None)
    ap.add_argument("--no-rerank", dest="rerank", action="store_false",
                    help="disable the cross-encoder reranker (first-stage RRF only)")
    args = ap.parse_args()

    filters = Filters(client=args.client, matter=args.matter,
                      doc_type=args.doc_type, author=args.author)
    hits = search(args.query, filters=filters, top_k=args.k, use_rerank=args.rerank)

    if not hits:
        console.print("[yellow]No results.[/]")
        return

    tag = "reranked" if hits[0].reranked else "RRF only"
    console.print(f"[dim]ranking: {tag}[/]")
    for i, h in enumerate(hits, 1):
        body = h.content.strip()
        if len(body) > 600:
            body = body[:600] + " ..."
        score_str = (f"rerank {h.score:.3f} · rrf {h.rrf_score:.4f}"
                     if h.reranked else f"rrf {h.score:.4f}")
        header = f"[bold]{i}. {h.citation()}[/]  ({score_str})"
        sub = " · ".join(x for x in [h.client, h.matter, h.author, h.doc_date] if x)
        console.print(Panel(body, title=header, subtitle=sub or None,
                            title_align="left", subtitle_align="left"))


if __name__ == "__main__":
    main()
