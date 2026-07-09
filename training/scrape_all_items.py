#!/usr/bin/env python
"""All-Item 8-K grab (demo): for each company, pull EVERY 8-K (all Item types),
save the full disclosure text (bucket A = format corpus) and classify attached
exhibits, recording per-Item whether a source document exists (bucket B = pairs).

Reports Item-frequency and per-Item pairability so we can size the full run.
"""
import html as htmllib
import json
import re
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import os
UA = "Richtech Legal RAG research tiger.l@richtechsystem.com"
OUT = Path(os.getenv("CORPUS_DIR", Path(__file__).resolve().parent / "corpus"))
OUT.mkdir(parents=True, exist_ok=True)
MANIFEST = OUT / "manifest.jsonl"

# Production tranche 1 — 95 EDGAR-validated tickers, weighted to small/mid-cap
# (the customer profile) with a few large caps as format references.
_TICKERS = ("SOUN BBAI SERV RGTI QUBT MVIS NNDM OUST KSCP OCGN ATOS INMB CADL ADTX "
            "VTVT ABVC ENVB TNXP MARA RIOT CIFR HUT BTBT WULF CLSK SLDP CENN DPRO O "
            "NNN ADC STAG PSTL GOOD LAND GNL CTO ROP DHR AME MYRG LNN AAPL MSFT JPM "
            "KO BARK HZO PRTS CULP BSET LQDT SGMO CRVS ADMA CRMD ANAB AKBA EYPT PGEN "
            "KPTI RIGL ARDX OCUL HROW VYGR ATNM ONCY SNGX PL RDW GFAI PRSO VUZI KOPN "
            "INVE WKEY PLUG FCEL BLNK CHPT RUN BEEM HYLN BTCS GREE SOS CAN SILA GIPR "
            "MDV AAT NAII FLWS LAKE").split()
_BIG = {"AAPL", "MSFT", "JPM", "KO", "O", "ROP", "DHR", "AME"}
_MID = {"MARA", "RIOT", "WULF", "CLSK", "STAG", "NNN", "ADC", "PLUG", "RUN", "CHPT",
        "LNN", "AAT", "MDV", "PL"}
COMPANIES = [(t, "big" if t in _BIG else "mid" if t in _MID else "small") for t in _TICKERS]
MAX_8K_PER_CO = 50

ITEM_TITLE = {
    "1.01": "Entry into Material Agreement", "1.02": "Termination of Material Agreement",
    "1.03": "Bankruptcy", "2.01": "Acquisition/Disposition of Assets",
    "2.02": "Results of Operations", "2.03": "Direct Financial Obligation",
    "2.04": "Triggering Events (obligations)", "2.05": "Exit/Disposal Costs",
    "2.06": "Material Impairments", "3.01": "Delisting / Listing-rule notice",
    "3.02": "Unregistered Sale of Equity", "3.03": "Modification of Holder Rights",
    "4.01": "Change in Accountant", "4.02": "Non-Reliance on Prior Financials",
    "5.01": "Change in Control", "5.02": "Director/Officer Changes",
    "5.03": "Amendments to Charter/Bylaws", "5.05": "Code of Ethics",
    "5.07": "Submission to Shareholder Vote", "5.08": "Shareholder Nominations",
    "6.01": "ABS Informational", "7.01": "Regulation FD", "8.01": "Other Events",
    "9.01": "Financial Statements & Exhibits",
}
# which exhibit type typically is the "source document" for each item
ITEM_SOURCE_EX = {
    "1.01": ("EX-10", "EX-2", "EX-4"), "1.02": ("EX-10",), "2.03": ("EX-10", "EX-4"),
    "3.02": ("EX-10", "EX-4"), "2.01": ("EX-2", "EX-10"), "5.02": ("EX-10",),
    "2.02": ("EX-99",), "7.01": ("EX-99",), "8.01": ("EX-99",),
}
EX_RE = re.compile(r"EX-(\d+)(?:\.(\d+))?", re.I)
_REDACT = re.compile(r"\[\*{2,}\]|\[Redacted\]|[▀-▟■-◿]{2,}|\*{4,}", re.I)
_OMIT = re.compile(r"omitted pursuant to|certain (identified )?information|has been omitted", re.I)


def fetch(url, is_json=False, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                d = r.read()
            time.sleep(0.13)
            return json.loads(d) if is_json else d.decode("utf-8", "replace")
        except Exception:
            if a == retries - 1:
                raise
            time.sleep(1 + a)


def to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = htmllib.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r" *\n *", "\n", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def classify_ex(index_html):
    """Return list of (ex_class, url) for substantive 8-K exhibits."""
    keep = {"1", "2", "3", "4", "10", "99"}  # skip 101(XBRL)/104(cover)/31/32
    out, seen = [], set()
    for row in re.split(r"(?i)<tr", index_html):
        m = EX_RE.search(row)
        if not m:
            continue
        major = m.group(1)
        if major not in keep:
            continue
        href = re.search(r'href="([^"]+)"', row)
        if not href:
            continue
        u = href.group(1)
        u = u if u.startswith("http") else "https://www.sec.gov" + u
        if u in seen:
            continue
        seen.add(u)
        out.append((f"EX-{major}", u))
    return out


def main():
    tmap = {r["ticker"].upper(): (r["cik_str"], r["title"])
            for r in fetch("https://www.sec.gov/files/company_tickers.json", is_json=True).values()}
    done = set()
    if MANIFEST.exists():
        done = {json.loads(l)["accession"] for l in MANIFEST.read_text().splitlines() if l.strip()}
    fh = open(MANIFEST, "a")
    for tk, size in COMPANIES:
        if tk not in tmap:
            print(f"[{tk}] not found", flush=True); continue
        cik, title = tmap[tk]
        sub = fetch(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", is_json=True)
        rec = sub.get("filings", {}).get("recent", {})
        forms, accs = rec.get("form", []), rec.get("accessionNumber", [])
        dates, items, prims = rec.get("filingDate", []), rec.get("items", []), rec.get("primaryDocument", [])
        idxs = [i for i in range(len(forms)) if forms[i] in ("8-K", "8-K/A")][:MAX_8K_PER_CO]
        cdir = OUT / tk
        cdir.mkdir(exist_ok=True)
        print(f"[{tk}] {title} — {len(idxs)} 8-Ks", flush=True)
        for i in idxs:
            acc = accs[i]
            if acc in done:
                continue
            acc_nd = acc.replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}"
            its = [x.strip() for x in (items[i] or "").split(",") if x.strip()]
            row = {"ticker": tk, "size": size, "cik": cik, "accession": acc,
                   "date": dates[i], "form": forms[i], "items": its}
            try:
                full = to_text(fetch(f"{base}/{prims[i]}")) if prims[i] else ""
                if full:
                    (cdir / f"{acc}_8k.txt").write_text(full[:400000])
                row["full_chars"] = len(full)
                idx_html = fetch(f"{base}/{acc}-index.htm")
                exs = classify_ex(idx_html)
                ex_classes = sorted({c for c, _ in exs})
                row["exhibits"] = ex_classes
                # grab text of substantive exhibits (cap) + redaction check
                ex_text_by_class = {}
                for c, u in exs:
                    if c in ("EX-1", "EX-2", "EX-4", "EX-10", "EX-99"):
                        t = to_text(fetch(u))
                        (cdir / f"{acc}_{c.replace('-','_')}_{u.rsplit('/',1)[-1][:40]}.txt").write_text(t[:400000])
                        ex_text_by_class.setdefault(c, t)
                # per-item pairability
                pair = {}
                for it in its:
                    src = ITEM_SOURCE_EX.get(it)
                    if not src:
                        pair[it] = {"pairable": False, "reason": "event-driven / no source doc"}
                        continue
                    match = next((c for c in src if c in ex_text_by_class), None)
                    if not match:
                        pair[it] = {"pairable": False, "reason": "no matching exhibit attached"}
                    else:
                        t = ex_text_by_class[match]
                        red = bool(_REDACT.search(t) or _OMIT.search(t))
                        pair[it] = {"pairable": not red, "source_ex": match,
                                    "redacted": red, "src_chars": len(t)}
                row["pairing"] = pair
                row["error"] = ""
            except Exception as e:
                row["error"] = str(e)[:150]
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
        print(f"[{tk}] done", flush=True)
    fh.close()
    print("\nDONE ->", MANIFEST, flush=True)


if __name__ == "__main__":
    main()
