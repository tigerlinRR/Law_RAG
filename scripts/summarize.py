#!/usr/bin/env python
"""Run a due-diligence review on a contract (PDF/Word): summary + key clauses + risks.

Usage:
  python scripts/summarize.py <path-to-contract> [--json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lawrag.summarize import review_contract

console = Console()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of a report")
    args = ap.parse_args()

    with console.status(f"Reviewing {args.path.name} ..."):
        r = review_contract(args.path)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    src = r.get("_source", args.path.name)
    console.print(Panel(r.get("summary", ""),
                        title=f"[bold]Due-Diligence Review — {src}[/]  ({r.get('doc_type','')})",
                        title_align="left"))
    if r.get("parties"):
        console.print("[bold]Parties:[/] " + "; ".join(r["parties"]))

    table = Table("Clause", "Value", "Source quote", show_lines=True)
    for cl in r.get("clauses", []):
        val = cl.get("value", "")
        style = "dim" if val.strip().lower() in ("", "not found") else ""
        quote = cl.get("quote", "")
        if len(quote) > 160:
            quote = quote[:160] + " ..."
        table.add_row(f"[{style}]{cl.get('name','')}[/]" if style else cl.get("name", ""),
                      val, f"[dim]{quote}[/]" if quote else "")
    console.print(table)

    risks = r.get("key_risks", [])
    if risks:
        console.print(Panel("\n".join(f"• {x}" for x in risks),
                            title="[bold red]Key risks to review[/]", title_align="left"))


if __name__ == "__main__":
    main()
