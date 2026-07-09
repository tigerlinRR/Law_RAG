#!/usr/bin/env python
"""Prepare train/val splits for the 8-K LoRA adapter from the packaged pairs.

Reads training/dataset/train_pairs.jsonl.gz (gunzips automatically), filters to the
subset you want to train on, splits BY COMPANY (so no company appears in both train
and val — prevents leakage / inflated eval), and writes train.jsonl + val.jsonl.

Defaults to the high-value CONTRACT family (substantive "source doc -> disclosure"
pairs), dropping incorporation-by-reference stubs and multi-exhibit filings whose
input is ambiguous. Flags let you widen the set.

Usage:
  python prepare_data.py                        # contract-family clean core
  python prepare_data.py --include-multi-source # + multi-exhibit filings
  python prepare_data.py --include-news         # + 2.02/7.01/8.01 (press-release family)
  python prepare_data.py --max-input-chars 15000
"""
import argparse
import gzip
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "dataset" / "train_pairs.jsonl.gz"
OUT_DIR = HERE / "dataset"


def load(path: Path):
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rt", encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-multi-source", action="store_true",
                    help="include filings with >1 substantive exhibit (input may be partial)")
    ap.add_argument("--include-stubs", action="store_true",
                    help="include incorporation-by-reference stub disclosures")
    ap.add_argument("--include-news", action="store_true",
                    help="include the press-release family (Items 2.02/7.01/8.01)")
    ap.add_argument("--max-input-chars", type=int, default=15000,
                    help="cap source-doc input so prompt+output fits the trainer's seq len")
    ap.add_argument("--val-frac", type=float, default=0.12,
                    help="fraction of COMPANIES held out for validation")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    rows = load(SRC)
    kept = []
    for r in rows:
        m = r["meta"]
        if m["task_family"] == "news" and not args.include_news:
            continue
        if m["task_family"] not in ("contract", "news"):
            continue
        if m.get("is_stub") and not args.include_stubs:
            continue
        if m.get("multi_source") and not args.include_multi_source:
            continue
        inp = r["input"][:args.max_input_chars]
        kept.append({"instruction": r["instruction"], "input": inp, "output": r["output"],
                     "ticker": m["ticker"], "item": m["item"], "family": m["task_family"],
                     "deal_type": m.get("deal_type")})

    # split by company
    companies = sorted({r["ticker"] for r in kept})
    rng = random.Random(args.seed)
    rng.shuffle(companies)
    n_val = max(1, int(len(companies) * args.val_frac))
    val_cos = set(companies[:n_val])
    train = [r for r in kept if r["ticker"] not in val_cos]
    val = [r for r in kept if r["ticker"] in val_cos]

    (OUT_DIR / "train.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in train))
    (OUT_DIR / "val.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in val))

    from collections import Counter
    fam = Counter(r["family"] for r in kept)
    print(f"kept {len(kept)} pairs  (families: {dict(fam)})")
    print(f"train: {len(train)} pairs from {len(companies)-n_val} companies")
    print(f"val:   {len(val)} pairs from {n_val} held-out companies -> {sorted(val_cos)}")
    print(f"wrote {OUT_DIR/'train.jsonl'} and {OUT_DIR/'val.jsonl'}")


if __name__ == "__main__":
    main()
