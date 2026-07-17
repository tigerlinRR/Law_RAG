#!/usr/bin/env python
"""Download Richtech's press-release / supplement exhibits (EX-99.*) from SEC EDGAR so the
multi-document 8-K flow can be tested with real news-Item (8.01/7.01) source material.

We already have the contract exhibits (EX-10.1 / EX-1.1) in data/RR contracts/. This grabs
the OTHER exhibits — mainly press releases (EX-99.1) attached to the 8.01/8-K filings — and
saves them as plain text under data/RR supplements/ (gitignored). Uses EDGAR's fair-access
path: a declared User-Agent, light rate limiting, no browser automation.
"""
import html
import re
import time
import urllib.request
from pathlib import Path

CIK = "1963685"  # Richtech Robotics Inc.
UA = "Richtech Law_RAG research tiger.l@richtechsystem.com"
OUT = Path("data/RR supplements")

# (filing date, accession) for filings that carry a press release / other supplement exhibit.
FILINGS = [
    ("2023-12-29", "0001213900-23-100084"),
    ("2024-09-05", "0001213900-24-076143"),
    ("2024-10-22", "0001213900-24-089677"),
    ("2025-06-30", "0001213900-25-059641"),
    ("2025-08-29", "0001213900-25-082097"),
    ("2025-11-17", "0001213900-25-111197"),
    ("2026-01-30", "0001213900-26-009823"),
    ("2026-05-28", "0001213900-26-062172"),
    ("2026-06-03", "0001213900-26-064448"),
    ("2025-12-05", "0001213900-25-118715"),  # 5.02 (director/officer) — any 99.x
]
# Exhibit filename patterns to fetch (press releases + other 99-series supplements).
WANT = re.compile(r"ex[-_]?99", re.I)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _to_text(raw: bytes) -> str:
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    import json
    got = 0
    for date, acc in FILINGS:
        accn = acc.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accn}"
        try:
            idx = json.loads(_get(base + "/index.json"))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {date} {acc}: index fetch failed ({e})")
            continue
        files = [f["name"] for f in idx.get("directory", {}).get("item", [])]
        exhibits = [n for n in files if WANT.search(n) and n.lower().endswith((".htm", ".html", ".txt"))]
        if not exhibits:
            print(f"  - {date} {acc}: no EX-99 exhibit found (files: {len(files)})")
            continue
        for name in exhibits:
            try:
                raw = _get(f"{base}/{name}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {date} {name}: download failed ({e})")
                continue
            text = _to_text(raw)
            dest = OUT / f"{date}_{acc}_{name}.txt"
            dest.write_text(text, encoding="utf-8")
            print(f"  ✓ {dest.name}  ({len(text)} chars)")
            got += 1
            time.sleep(0.4)  # be polite to EDGAR
        time.sleep(0.4)
    print(f"\nDone: {got} supplement exhibit(s) -> {OUT}")


if __name__ == "__main__":
    main()
