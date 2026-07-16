#!/usr/bin/env python
"""Delexicalize 8-K pairs for v4 — mask DEAL-SPECIFIC FACTS only, PRESERVE legal
boilerplate/structure (Securities Exchange Act, Form 8-K, Item/Exhibit/Section...).

Masks: amounts, share/counts, percentages, dates, company names (corporate-suffix
regex), person names (spaCy PERSON + stoplist). Consistent typed indexed placeholders
across instruction+input+output. Backfill happens on Jetson.
Usage: python delex.py --sample 3 | python delex.py
"""
import argparse, gzip, json, os, re, random
from pathlib import Path
import spacy

# Env-overridable so RTX can point at the filtered v5 dataset without editing this file:
#   DELEX_SRC=training/dataset/train_pairs_delex_filtered.jsonl.gz DELEX_OUT=/mnt/raid/law_rag_8k/data_v5 python delex.py
SRC = Path(os.getenv("DELEX_SRC", "/home/thematrix/Law_RAG/training/dataset/train_pairs.jsonl.gz"))
OUT = Path(os.getenv("DELEX_OUT", "/mnt/raid/law_rag_8k/data_v4"))
SYSTEM = ("You are a securities lawyer drafting U.S. SEC Form 8-K Item disclosures. "
          "Write in the concise, neutral style of a real filing, disclosing only "
          "material terms and using only facts present in the provided source document.")

nlp = spacy.load("en_core_web_sm", disable=["lemmatizer", "tagger", "parser", "attribute_ruler"])
nlp.max_length = 2_000_000

MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
CORP = r"(?:Inc|L\.?L\.?C|Corp|Corporation|Company|Co|Ltd|L\.?P|LP|PLC|N\.?A|Holdings|Partners|Capital|Group|Trust|Bank)"
REGEX = [
    ("DATE",   re.compile(rf"{MONTHS}\s+\d{{1,2}},?\s+\d{{4}}")),
    ("DATE",   re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("DATE",   re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("AMOUNT", re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|thousand))?", re.I)),
    ("PCT",    re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent)\b", re.I)),
    ("NUM",    re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b")),
    # company: a capitalized run ending in a corporate suffix. Connectors are space/comma/&
    # ONLY (no newline) and every token must start with a LETTER — so the match cannot cross
    # a sentence/line boundary or swallow an adjacent date ("On\nAugust 20, 2024, Foo Inc.").
    ("ORG",    re.compile(rf"\b[A-Z][\w&.\-]*(?:[ \t,&]+(?:[A-Z][\w&.\-]*|and)){{0,6}}[ \t,]+{CORP}\b\.?")),
]
# lossless numeric/date normalization so format variants share ONE placeholder
# ("$38.7 million" == "$38,700,000", "August 20, 2024" == "2024-08-20"). This is
# VALUE-preserving (not byte-preserving): a within-doc variant collision backfills to the
# first surface's canonical form — same value, normalized format. Fuzzy/rounding is
# deliberately NOT merged (would conflate distinct material figures — unsafe to backfill).
_UNIT = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_MIDX = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
MAX_INPUT = 24000  # source-doc window (chars) — training AND inference must use the same
# legal / structural boilerplate that must NEVER be masked (keep as learnable structure)
STOP = {w.lower() for w in [
    "item", "exhibit", "section", "company", "report", "managers", "manager", "board",
    "annex", "schedule", "commission", "sec", "securities", "act", "exchange act",
    "securities act", "securities exchange act", "exchange", "form", "current report",
    "registration statement", "common stock", "preferred stock", "class a", "class b",
    "purchase agreement", "agreement", "notes", "sarbanes-oxley", "regulation fd",
    "gaap", "irs", "delaware", "nasdaq", "nyse", "committee", "compensation committee",
    "the company", "buyer", "seller", "purchaser", "issuer", "escrow", "closing", "note",
]}

def canon_num(s):
    """Value canon: expand k/m/b units, drop $/commas/spaces/trailing words -> a decimal
    string, so '$38.7 million' == '38,700,000'. Falls back to the old strip on parse failure."""
    t = s.lower()
    mult = 1.0
    m = re.search(r"(thousand|million|billion)", t)
    if m:
        mult = _UNIT[m.group(1)]
        t = t[:m.start()]
    d = re.sub(r"[^0-9.]", "", t)
    if d not in ("", "."):
        try:
            v = float(d) * mult
            return str(int(v)) if v == int(v) else ("%f" % v).rstrip("0").rstrip(".")
        except ValueError:
            pass
    return re.sub(r"[,\s]", "", s.lower()).rstrip(".")


def canon_date(s):
    """Normalize a date surface to ISO YYYY-MM-DD so formats share one placeholder."""
    x = s.lower()
    m = re.search(rf"({'|'.join(_MIDX)})\s+(\d{{1,2}}),?\s+(\d{{4}})", x)
    if m:
        return f"{int(m.group(3)):04d}-{_MIDX[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", x)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", x)
    if m:
        y = int(m.group(3)) + (2000 if int(m.group(3)) < 100 else 0)
        return f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return x
def canon_name(s):
    s = re.sub(r"[,\.]", "", s.lower()).strip()
    s = re.sub(rf"\b(inc|llc|ltd|corp|corporation|company|co|lp|plc|the|holdings|partners|capital|group|trust|bank|na)\b", "", s).strip()
    return re.sub(r"\s+", " ", s)
def is_stop(txt):
    t = canon_name(txt)
    return (not t) or t in STOP or any(t == s or t in s.split() for s in STOP) or len(t) < 3

def collect(text):
    spans = []
    for typ, pat in REGEX:
        for m in pat.finditer(text):
            if typ == "ORG":
                if is_stop(m.group()): continue
                spans.append([m.start(), m.end(), "ORG", canon_name(m.group())])
            elif typ == "DATE":
                spans.append([m.start(), m.end(), "DATE", canon_date(m.group())])
            else:
                spans.append([m.start(), m.end(), typ, canon_num(m.group())])
    for e in nlp(text).ents:
        if e.label_ == "PERSON" and not is_stop(e.text) and len(e.text.split()) >= 2:
            spans.append([e.start_char, e.end_char, "PERSON", canon_name(e.text)])
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    kept, last = [], -1
    for s in spans:
        if s[0] >= last: kept.append(s); last = s[1]
    return kept

def delexer():
    reg, cnt = {}, {}
    cores = {}  # ORG canon -> core surface (for bare-name second pass)
    def ph(typ, can):
        k = (typ, can)
        if k not in reg:
            cnt[typ] = cnt.get(typ, 0) + 1; reg[k] = f"[{typ}_{cnt[typ]}]"
        return reg[k]
    def run(text):
        for s, e, typ, can in sorted(collect(text), key=lambda x: -x[0]):
            if typ == "ORG": cores.setdefault(can, text[s:e])
            text = text[:s] + ph(typ, can) + text[e:]
        # bare-name second pass: mask standalone core names (e.g. "SoundHound AI" without suffix)
        for can, surf in cores.items():
            core = re.sub(rf"\s+{CORP}\.?$", "", surf).strip()
            if len(core) >= 4:
                text = re.sub(r"\b" + re.escape(core) + r"\b", ph("ORG", can), text)
        return text
    return run, reg
def process(r, max_input=MAX_INPUT):
    # INPUT-FIRST numbering: delex the source BEFORE the output so input entities claim the
    # low indices. Inference (delex_backfill.delex_source) also numbers input-only, so a
    # placeholder the model copies resolves to the same source value. (v4 numbered OUTPUT
    # first -> indices drifted with many entities -> wrong-org backfill; this is that fix.)
    run, reg = delexer()
    inp = run(r["input"][:max_input]); out = run(r["output"]); instr = run(r["instruction"])
    return {"instruction": instr, "input": inp, "output": out}, reg

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--sample", type=int, default=0)
    args = ap.parse_args()
    rows = [json.loads(l) for l in gzip.open(SRC, "rt", encoding="utf-8") if l.strip()]
    if args.sample:
        for r in rows[:args.sample]:
            dl, reg = process(r)
            print("=" * 70)
            print(f"[{r['meta']['ticker']} Item {r['meta']['item']}]  ({len(reg)} entities masked)")
            print("--- OUTPUT (delexed) ---\n" + dl["output"][:750])
        return
    OUT.mkdir(parents=True, exist_ok=True)
    kept = []
    for i, r in enumerate(rows):
        dl, _ = process(r); kept.append({**dl, "ticker": r["meta"]["ticker"]})
        if (i + 1) % 200 == 0: print(f"  delexed {i+1}/{len(rows)}", flush=True)
    cos = sorted({k["ticker"] for k in kept}); random.Random(13).shuffle(cos)
    nval = max(1, int(len(cos) * 0.12)); valco = set(cos[:nval])
    def to_lf(r):
        return {"system": SYSTEM, "instruction": r["instruction"] + "\n\n=== SOURCE DOCUMENT ===\n" + r["input"],
                "input": "", "output": r["output"]}
    tr = [to_lf(r) for r in kept if r["ticker"] not in valco]
    va = [to_lf(r) for r in kept if r["ticker"] in valco]
    (OUT / "lawrag_8k_v4_train.json").write_text(json.dumps(tr, ensure_ascii=False, indent=0))
    (OUT / "lawrag_8k_v4_val.json").write_text(json.dumps(va, ensure_ascii=False, indent=0))
    info = {n: {"file_name": f"lawrag_8k_v4_{s}.json", "columns": {"prompt": "instruction",
            "query": "input", "response": "output", "system": "system"}}
            for n, s in [("lawrag_8k_v4_train", "train"), ("lawrag_8k_v4_val", "val")]}
    (OUT / "dataset_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"DONE: train {len(tr)} / val {len(va)} -> {OUT}")

if __name__ == "__main__":
    main()
