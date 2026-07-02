#!/usr/bin/env python
"""Ingest a file or folder into the knowledge base.

Usage:
  python scripts/ingest.py <path> [--client X] [--matter Y] [--doc-type S-8]
                                  [--author "Jane Doe"] [--doc-date 2021-05-01]

Metadata flags apply to every file in the batch. (Per-file metadata / auto
extraction is a later enhancement.)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.table import Table

from lawrag import db
from lawrag.ingest import DocMeta, ingest_file, iter_files

console = Console()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--client")
    ap.add_argument("--matter")
    ap.add_argument("--doc-type", dest="doc_type")
    ap.add_argument("--author")
    ap.add_argument("--doc-date", dest="doc_date")
    ap.add_argument("--filing-item", dest="filing_item",
                     help="SEC 8-K Item number, e.g. 1.01 (manual override)")
    args = ap.parse_args()

    db.init_schema()
    meta = DocMeta(doc_type=args.doc_type, client=args.client, matter=args.matter,
                   author=args.author, doc_date=args.doc_date)
    if args.filing_item:
        meta.extra["filing_item"] = args.filing_item

    files = iter_files(args.path)
    if not files:
        console.print("[yellow]No .pdf/.docx files found.[/]")
        return

    table = Table("File", "Status", "Chunks", "Detail")
    counts: dict[str, int] = {}
    with console.status(f"Ingesting {len(files)} file(s)..."):
        for f in files:
            r = ingest_file(f, meta)
            counts[r.status] = counts.get(r.status, 0) + 1
            color = {"ingested": "green", "skipped_duplicate": "cyan",
                     "needs_ocr": "yellow", "error": "red"}.get(r.status, "white")
            table.add_row(f.name, f"[{color}]{r.status}[/]",
                          str(r.n_chunks or ""), r.detail)

    db.ensure_vector_index()
    console.print(table)
    console.print("Summary:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
