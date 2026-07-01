#!/usr/bin/env python
"""Batch due-diligence: review every contract in a folder, export Excel + Word.

Usage:
  python scripts/dd_batch.py <folder> [--excel out.xlsx] [--word out.docx]
Defaults write due_diligence.xlsx and due_diligence.docx next to the folder.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from lawrag import export
from lawrag.ingest import iter_files
from lawrag.summarize import review_contract

console = Console()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--excel", type=Path)
    ap.add_argument("--word", type=Path)
    args = ap.parse_args()

    files = iter_files(args.folder)
    if not files:
        console.print("[yellow]No .pdf/.docx files found.[/]")
        return

    reviews = []
    for i, f in enumerate(files, 1):
        console.print(f"[dim]({i}/{len(files)})[/] reviewing {f.name} …")
        try:
            reviews.append(review_contract(f))
        except Exception as e:  # noqa: BLE001 — skip and continue the batch
            console.print(f"  [red]skipped[/]: {e}")

    if not reviews:
        console.print("[red]No contracts could be reviewed.[/]")
        return

    base = args.folder if args.folder.is_dir() else args.folder.parent
    xlsx = args.excel or base / "due_diligence.xlsx"
    docx_out = args.word or base / "due_diligence.docx"
    xlsx.write_bytes(export.to_excel(reviews))
    docx_out.write_bytes(export.to_word(reviews))
    console.print(f"[green]Reviewed {len(reviews)} contract(s).[/]")
    console.print(f"  Excel: {xlsx}\n  Word:  {docx_out}")


if __name__ == "__main__":
    main()
