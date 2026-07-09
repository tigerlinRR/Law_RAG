#!/usr/bin/env python
"""Turn the raw scraped corpus (data/multico_all) into clean instruction-tuning
pairs for fine-tuning: (source document -> the real Item disclosure it produced).

Deterministic only (no LLM): slice each Item's disclosure section out of the full
8-K, pair it with its source exhibit, clean EDGAR artifacts, tag task family +
deal type, and emit train_pairs.jsonl. Prints stats + writes readable samples.
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import os
ROOT = Path(os.getenv("CORPUS_DIR", Path(__file__).resolve().parent / "corpus"))
MANIFEST = ROOT / "manifest.jsonl"
OUT = ROOT / "train_pairs.jsonl"
SAMPLES = ROOT / "train_samples.txt"

ITEM_TITLE = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.03": "Creation of a Direct Financial Obligation",
    "3.02": "Unregistered Sales of Equity Securities",
    "5.02": "Departure/Election of Directors or Officers",
    "2.02": "Results of Operations and Financial Condition",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
}
CONTRACT_FAMILY = {"1.01", "1.02", "2.01", "2.03", "3.02", "5.02"}
NEWS_FAMILY = {"2.02", "7.01", "8.01"}
SRC_NOUN = {"EX-10": "agreement", "EX-2": "agreement", "EX-4": "instrument",
            "EX-1": "agreement", "EX-99": "press release"}

MAX_INPUT_CHARS = 24000   # cap source doc for training input (Qwen ctx budget)
MIN_OUTPUT_CHARS = 150    # skip trivial/empty disclosures
MAX_OUTPUT_CHARS = 12000

_PAGE = re.compile(r"^\s*\d{1,3}\s*$", re.M)
_TOC = re.compile(r"(?im)^\s*table of contents\s*$")


_FNAME = re.compile(r"(?im)^\s*(EX-[\d.]+|[\w-]+\.htm[l]?)\s*$")


def clean(t: str) -> str:
    t = t.replace("\f", "\n")
    t = _TOC.sub("", t)
    t = _PAGE.sub("", t)                       # bare page-number lines
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# A real next-Item heading starts a line; an inline cross-reference ("the
# information in Item 1.01 above is incorporated…") does not, and is typically
# followed by words like "above"/"below"/"of this". Anchor to line start and
# reject those to avoid truncating a disclosure at its own cross-reference.
_HEAD_RE = re.compile(r"(?m)^\s*Item[\s ]+(\d\.\d\d)\b[.\s]*(\w+)?")


def slice_item(full_text: str, item: str) -> str:
    heads = [(m.start(), m.group(1), (m.group(2) or "").lower())
             for m in _HEAD_RE.finditer(full_text)]
    real = [(s, it) for s, it, nxt in heads
            if nxt not in ("above", "below", "of", "hereof", "hereto")]
    for k, (start, it) in enumerate(real):
        if it != item:
            continue
        end = len(full_text)
        for s2, it2 in real[k + 1:]:
            if it2 != item and s2 > start + 20:
                end = s2
                break
        sig = re.search(r"(?im)^\s*SIGNATURE\s*$", full_text[start:end])
        if sig:
            end = start + sig.start()
        return full_text[start:end].strip()
    return ""


def strip_ex_header(t: str) -> str:
    """Drop the leading filename / 'EX-10.1' lines the scrape left at the top."""
    lines = t.split("\n")
    while lines and _FNAME.match(lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()


_STUB_RE = re.compile(r"incorporated herein by reference", re.I)


def is_stub(output: str) -> bool:
    """A near-empty disclosure that just incorporates an exhibit by reference."""
    return len(output) < 450 and bool(_STUB_RE.search(output))


def deal_type(text: str) -> str:
    t = text.lower()
    tests = [
        ("real estate", ("purchase and sale agreement", "real property", "square feet",
                          "square foot", "lease agreement")),
        ("note / debt", ("promissory note", "senior note", "convertible note",
                          "principal amount", "indenture", "credit agreement", "loan")),
        ("equity financing", ("securities purchase agreement", "registered direct",
                              "private placement", "at-the-market", "at the market",
                              "standby equity", "underwriting agreement", "warrant", "pipe")),
        ("M&A / asset", ("merger agreement", "asset purchase", "stock purchase agreement",
                          "business combination", "arrangement agreement")),
        ("services / commercial", ("master services", "services agreement", "supply agreement",
                                   "collaboration", "license agreement", "distribution")),
        ("officer / director", ("employment agreement", "separation", "appointed", "resigned",
                                "director", "chief executive", "chief financial")),
    ]
    for label, kws in tests:
        if any(k in t for k in kws):
            return label
    return "unclassified"


def find_source_file(acc: str, tk: str, src_ex: str) -> str:
    cls = src_ex.replace("-", "_")
    hits = sorted((ROOT / tk).glob(f"{acc}_{cls}_*.txt"), key=lambda p: -p.stat().st_size)
    return hits[0].read_text() if hits else ""


def instruction(item: str, family: str, noun: str) -> str:
    title = ITEM_TITLE.get(item, "")
    if family == "contract":
        return (f"You are a securities lawyer drafting a U.S. SEC Form 8-K. Based solely on "
                f"the following {noun}, draft the Item {item} ({title}) disclosure. Disclose "
                f"only the terms that are material to the registrant, in the concise, neutral "
                f"style of a real 8-K, and close with the standard exhibit incorporation-by-"
                f"reference sentence. Use only facts present in the {noun}.")
    return (f"You are a securities lawyer drafting a U.S. SEC Form 8-K. Based on the following "
            f"{noun}, draft the Item {item} ({title}) disclosure in the concise, neutral style "
            f"of a real 8-K, using only the facts provided.")


def main():
    rows = [json.loads(l) for l in MANIFEST.read_text().splitlines() if l.strip()]
    fam_count = Counter()
    item_count = Counter()
    deal_count = Counter()
    in_lens, out_lens = [], []
    written = 0
    truncated = 0
    n_stub = 0
    n_multi = 0
    contract_clean = 0
    samples = []

    with open(OUT, "w") as fh:
        for r in rows:
            acc, tk = r["accession"], r["ticker"]
            pairing = r.get("pairing") or {}
            if not any(p.get("pairable") for p in pairing.values()):
                continue
            f8k_path = ROOT / tk / f"{acc}_8k.txt"
            if not f8k_path.exists():
                continue
            full = clean(f8k_path.read_text())
            for item, p in pairing.items():
                if not p.get("pairable"):
                    continue
                family = "contract" if item in CONTRACT_FAMILY else \
                         "news" if item in NEWS_FAMILY else "other"
                if family == "other":
                    continue
                src_ex = p.get("source_ex", "")
                src = strip_ex_header(clean(find_source_file(acc, tk, src_ex)))
                target = slice_item(full, item)
                if len(src) < 400 or len(target) < MIN_OUTPUT_CHARS:
                    continue
                if len(target) > MAX_OUTPUT_CHARS:
                    continue
                noun = SRC_NOUN.get(src_ex, "document")
                inp = src
                if len(inp) > MAX_INPUT_CHARS:
                    inp = inp[:MAX_INPUT_CHARS]
                    truncated += 1
                # classify on the disclosure (short, deal-focused) not the whole contract
                dt = deal_type(target) if family == "contract" else "n/a"
                # >1 substantive exhibit class -> the true source may be another exhibit
                subst = [e for e in (r.get("exhibits") or []) if e in ("EX-1", "EX-2", "EX-4", "EX-10")]
                stub = is_stub(target)
                ex = {
                    "instruction": instruction(item, family, noun),
                    "input": inp,
                    "output": target,
                    "meta": {"ticker": tk, "size": r.get("size"), "accession": acc,
                             "date": r.get("date"), "item": item, "task_family": family,
                             "source_ex": src_ex, "deal_type": dt, "is_stub": stub,
                             "multi_source": len(subst) > 1,
                             "input_full_chars": len(src), "output_chars": len(target)},
                }
                fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
                written += 1
                fam_count[family] += 1
                item_count[item] += 1
                if stub:
                    n_stub += 1
                if ex["meta"]["multi_source"]:
                    n_multi += 1
                if family == "contract":
                    deal_count[dt] += 1
                    if not stub and not ex["meta"]["multi_source"]:
                        contract_clean += 1
                in_lens.append(len(src))
                out_lens.append(len(target))
                if family == "contract" and not stub and not ex["meta"]["multi_source"] and len(samples) < 4:
                    samples.append(ex)

    print(f"Training pairs written: {written}  -> {OUT}")
    print(f"  truncated inputs (>{MAX_INPUT_CHARS} chars): {truncated}")
    print(f"  incorporation-by-reference stubs (flagged): {n_stub}")
    print(f"  multi-exhibit filings (source ambiguous, flagged): {n_multi}")
    print(f"\n*** HIGH-VALUE core: {contract_clean} contract-family pairs "
          f"(substantive, single-source, non-stub) ***")
    print(f"\nBy task family: {dict(fam_count)}")
    print(f"\nBy Item:")
    for it, c in sorted(item_count.items()):
        print(f"  {it}  {c:>5}  {ITEM_TITLE.get(it,'')}")
    print(f"\nContract-family deal types:")
    for d, c in deal_count.most_common():
        print(f"  {c:>4}  {d}")
    if in_lens:
        import statistics as st
        print(f"\nInput chars  — median {int(st.median(in_lens))}, max {max(in_lens)}")
        print(f"Output chars — median {int(st.median(out_lens))}, max {max(out_lens)}")

    with open(SAMPLES, "w") as s:
        for i, ex in enumerate(samples, 1):
            s.write(f"===== SAMPLE {i}  [{ex['meta']['ticker']} {ex['meta']['item']} "
                    f"{ex['meta']['deal_type']}] =====\n")
            s.write(f"--- INSTRUCTION ---\n{ex['instruction']}\n\n")
            s.write(f"--- INPUT (source doc, first 1200 chars) ---\n{ex['input'][:1200]}\n\n")
            s.write(f"--- OUTPUT (real Item {ex['meta']['item']} disclosure) ---\n{ex['output']}\n\n\n")
    print(f"\nWrote 3 readable samples -> {SAMPLES}")


if __name__ == "__main__":
    main()
