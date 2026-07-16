#!/usr/bin/env python
"""Task B — a COMPANY-NEUTRAL, data-derived materiality rubric from the multi-company
EDGAR corpus (~90 issuers), replacing the earlier 17-Richtech-filing rubric.

For each real Item disclosure (the `output` side of the training pairs), detect which
categories of terms it mentions (deterministic keyword/regex — no LLM, so it scales to the
whole corpus). Aggregate per Item into hit-rates = "what fraction of real filings across the
market disclose this category", overall and by deal type. Those market-norm rates are the
rubric bands (ALWAYS / USUALLY / RARELY), baked into draft.ITEM_RULES.

Run:  CORPUS=data/multico_all/train_pairs.jsonl ./.venv/bin/python training/build_general_rubric.py
"""
import json
import os
import re
from collections import defaultdict

CORPUS = os.getenv("CORPUS", "data/multico_all/train_pairs.jsonl")

# Category -> regex tested (case-insensitive) against the real disclosure text. Deliberately
# specific to reduce cross-category false hits; a directional prior, not a parser.
CATS = {
    "price / consideration":        r"\$|purchase price|consideration|aggregate (?:of|principal)|for a (?:total|purchase)",
    "asset size / description":     r"square f|sq\.?\s?ft|\bacres?\b|rentable|building|premises|parcel|shares of|units of",
    "term / duration / maturity":   r"\bmatur|\bterm\b|for a period of|expir|through (?:the )?(?:date|[A-Z][a-z]+ \d)",
    "deposit / earnest money":      r"earnest|\bdeposit\b|escrow",
    "interest rate / discount":     r"interest (?:rate|at)|per annum|\bcoupon\b|original issue discount|\bOID\b",
    "closing / completion timing":  r"closing|consummat|shall (?:close|be held)|completion|on or before|effective date of",
    "termination rights":           r"terminat",
    "conversion / redemption":      r"convert|conversion|redeem|redemption",
    "governing law":                r"governing law|governed by (?:the )?laws|laws of the state",
    "indemnification":              r"indemnif",
    "dispute resolution":           r"arbitrat|dispute resolution|exclusive jurisdiction|\bvenue\b|jury trial",
    "assignment / change-of-control": r"assign(?:ment|ed|s|able)?\b|change of control",
    "representations / warranties":  r"represent|warrant",
    "confidentiality":              r"confidential|non-disclosure",
    "'customary provisions' catch-all": r"customary",
}
COMPILED = {k: re.compile(v, re.I) for k, v in CATS.items()}


def band(rate):
    return "ALWAYS" if rate >= 0.80 else "USUALLY" if rate >= 0.30 else "RARELY"


def main():
    with open(CORPUS, encoding="utf-8") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    by_item = defaultdict(list)
    for r in rows:
        by_item[r["meta"]["item"]].append(r)

    for item in ("1.01", "2.03", "3.02"):
        pairs = by_item.get(item, [])
        if not pairs:
            continue
        n = len(pairs)
        hits = {c: 0 for c in CATS}
        by_deal = defaultdict(lambda: [0, defaultdict(int)])  # deal_type -> [count, {cat:hits}]
        for r in pairs:
            out = r.get("output", "")
            dt = r["meta"].get("deal_type", "n/a")
            by_deal[dt][0] += 1
            for c, rx in COMPILED.items():
                if rx.search(out):
                    hits[c] += 1
                    by_deal[dt][1][c] += 1
        print(f"\n{'='*72}\nItem {item} — {n} real disclosures across the corpus\n{'='*72}")
        ranked = sorted(hits.items(), key=lambda kv: -kv[1])
        for c, h in ranked:
            rate = h / n
            print(f"  [{band(rate):7}] {rate*100:5.1f}%  {c}")
        if item == "1.01":
            print("\n  --- by deal type (rate per category, for the larger buckets) ---")
            for dt, (cnt, dh) in sorted(by_deal.items(), key=lambda kv: -kv[1][0]):
                if cnt < 15:
                    continue
                top = sorted(dh.items(), key=lambda kv: -kv[1])
                cats = ", ".join(f"{c} {100*h//cnt}%" for c, h in top[:6])
                print(f"    {dt:22} (n={cnt}): {cats}")


if __name__ == "__main__":
    main()
