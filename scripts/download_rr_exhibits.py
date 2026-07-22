#!/usr/bin/env python
"""Download the FULL exhibit set for Richtech's 8-K filings from SEC EDGAR (test material).

Generalizes download_rr_supplements.py (which fetched only EX-99). Grabs every exhibit type
that appears across Richtech's 8-Ks and routes each to the local test dir by role:

  agreements / securities instruments  EX-1.x, EX-4.x, EX-10.x  -> data/RR contracts/   (drafting sources)
  opinions / consents / press releases EX-5.x, EX-23.x, EX-99.x -> data/RR supplements/

This is TEST material only: we replicate real Richtech filings to compare our drafts against
what was actually filed (the eventual product lets a user upload their own docs instead).
Accessions are derived from the reference 8-K PDFs in data/RR 8-K/. Uses EDGAR fair-access:
a declared User-Agent + light rate limiting, no browser automation. Everything lands under
data/ (gitignored). An exhibit already present (e.g. a curated EX-10.1 PDF) is skipped.
"""
import html
import re
import time
import urllib.request
from pathlib import Path

CIK = "1963685"  # Richtech Robotics Inc.
UA = "Richtech Law_RAG research tiger.l@richtechsystem.com"
CONTRACTS = Path("data/RR contracts")
SUPPLEMENTS = Path("data/RR supplements")

# Exhibit-number -> role. Contract-family exhibits are drafting sources (Item 1.01/2.03/3.02);
# supplement-family are furnished/derivative (news, opinions, consents) -> 9.01 index.
CONTRACT_TYPES = {"1", "4", "10"}
SUPPLEMENT_TYPES = {"5", "23", "99"}
EX_RE = re.compile(r"ex[-_]?(\d+)-(\d+)", re.I)  # ex10-1, ex-4.1, ex99-2, ...
ACC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_.*_(\d{10}-\d{2}-\d{6})\.pdf$")


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


def _accessions() -> list[tuple[str, str]]:
    """(filing_date, accession) for every reference 8-K PDF we hold."""
    out = []
    for p in sorted(Path("data/RR 8-K").glob("*.pdf")):
        m = ACC_RE.match(p.name)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _already_have(directory: Path, acc: str, typ: str, sub: str) -> bool:
    """True if this exhibit is already on disk (e.g. a curated EX-10.1 PDF), so we skip it."""
    toks = (f"ex{typ}-{sub}", f"ex-{typ}.{sub}")
    for f in directory.glob(f"*{acc}*"):
        name = f.name.lower()
        if any(tok in name for tok in toks):
            return True
    return False


def main() -> None:
    import json
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    SUPPLEMENTS.mkdir(parents=True, exist_ok=True)
    got = skipped = 0
    for date, acc in _accessions():
        accn = acc.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accn}"
        try:
            idx = json.loads(_get(base + "/index.json"))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {date} {acc}: index fetch failed ({e})")
            continue
        for f in idx.get("directory", {}).get("item", []):
            name = f["name"]
            if not name.lower().endswith((".htm", ".html", ".txt", ".pdf")):
                continue
            m = EX_RE.search(name)
            if not m:
                continue
            typ, sub = m.group(1), m.group(2)
            if typ in CONTRACT_TYPES:
                dest_dir = CONTRACTS
            elif typ in SUPPLEMENT_TYPES:
                dest_dir = SUPPLEMENTS
            else:
                continue
            if _already_have(dest_dir, acc, typ, sub):
                skipped += 1
                continue
            try:
                raw = _get(f"{base}/{name}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {date} {name}: download failed ({e})")
                continue
            is_pdf = name.lower().endswith(".pdf")
            stem = f"{date}_{acc}_EX-{typ}.{sub}_{Path(name).stem}"
            if is_pdf:
                dest = dest_dir / f"{stem}.pdf"
                dest.write_bytes(raw)
            else:
                dest = dest_dir / f"{stem}.txt"
                dest.write_text(_to_text(raw), encoding="utf-8")
            print(f"  ✓ {dest_dir.name}/{dest.name}")
            got += 1
            time.sleep(0.4)  # be polite to EDGAR
        time.sleep(0.3)
    print(f"\nDone: downloaded {got}, skipped {skipped} already-present.")


if __name__ == "__main__":
    main()
