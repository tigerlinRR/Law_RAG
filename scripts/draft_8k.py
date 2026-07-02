#!/usr/bin/env python
"""Draft an SEC Form 8-K Item disclosure from a contract (experimental).

Grounds the draft in facts extracted from the contract itself; prior 8-K filings
of the same Item type (if any are in the library) are used only as a structure/
style reference, never as a source of facts. Every fact in the draft is cited
back to its verbatim source quote in "facts_used" — review that list, not just
the prose, before trusting anything in the disclosure.

Usage:
  python scripts/draft_8k.py <path-to-contract> [--item 1.01] [--json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lawrag.draft import draft_8k

console = Console()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--item", default="1.01", help="8-K Item number, e.g. 1.01")
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of a report")
    args = ap.parse_args()

    with console.status(f"Drafting Item {args.item} disclosure from {args.path.name} ..."):
        r = draft_8k(args.path, item=args.item)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    console.print(Panel(
        r.get("disclosure", ""),
        title=f"[bold]Item {r.get('item')} — {r.get('item_title')}[/]  "
              f"(source: {r.get('_source_contract')})",
        title_align="left",
    ))

    precedents = r.get("_precedents_used") or []
    console.print("[bold]Precedents used (style reference only):[/] " +
                  ("; ".join(precedents) if precedents else "[dim]none found in library[/]"))

    table = Table("Fact", "Source quote", show_lines=True, title="Fact -> source trace")
    for f in r.get("facts_used", []):
        quote = f.get("source_quote", "")
        if len(quote) > 160:
            quote = quote[:160] + " ..."
        table.add_row(f.get("fact", ""), quote)
    console.print(table)
    console.print("[dim]Experimental: verify every row above against the source "
                  "contract before relying on this draft.[/]")


if __name__ == "__main__":
    main()
