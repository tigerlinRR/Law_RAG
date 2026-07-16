#!/usr/bin/env python
"""Filter raw 8-K training pairs to the delex-GROUNDABLE subset before training v5.

Why: measured (2026-07-16) that ~60% of output placeholders have NO anchor in the source
exhibit (facts paraphrased/rounded or drawn from outside the paired doc). Training a delex
adapter on those teaches it to emit placeholders that cannot be backfilled -> wrong-org /
[NOT IN SOURCE] failures. Keeping only pairs whose output placeholders are >= THRESHOLD
grounded in the input makes the supervision consistent = "only emit placeholders you can
copy". Uses the SAME delex logic (delex.py: input-first numbering, lossless num/date canon,
tightened ORG regex) that trains + backfills, at the SAME 24k window, so the metric matches
what the model will actually see.

Usage (from repo root):
  CORPUS=data/multico_all/train_pairs_full.jsonl \
  ./.venv/bin/python training/llamafactory/delex_filter.py --threshold 0.90 \
      --out data/multico_all/train_pairs_delex_filtered.jsonl
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delex as dx  # noqa: E402  (loads spaCy)

CONTRACT = {"1.01", "1.02", "2.01", "2.03", "3.02", "5.02"}


def groundable_frac(pair, window):
    """Fraction of the OUTPUT's placeholder occurrences whose (type, canon) also appears in
    the INPUT window — i.e. can be backfilled from the source. 1.0 if the output masks none."""
    out_sp = dx.collect(pair["output"])
    in_keys = {(t, c) for (s, e, t, c) in dx.collect(pair["input"][:window])}
    if not out_sp:
        return 1.0, 0
    hit = sum(1 for (s, e, t, c) in out_sp if (t, c) in in_keys)
    return hit / len(out_sp), len(out_sp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.getenv("CORPUS", "data/multico_all/train_pairs_full.jsonl"))
    ap.add_argument("--out", default="data/multico_all/train_pairs_delex_filtered.jsonl")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--window", type=int, default=dx.MAX_INPUT)
    args = ap.parse_args()

    with open(args.corpus, encoding="utf-8") as fh:  # split on \n only (docs contain \x0b/ )
        rows = [json.loads(l) for l in fh if l.strip()]
    print(f"corpus={len(rows)}  window={args.window}  threshold={args.threshold}", flush=True)

    by_item = defaultdict(lambda: [0, 0])   # item -> [kept, total]
    fam_kept = defaultdict(int)
    kept = []
    sum_g = 0.0
    for i, r in enumerate(rows):
        frac, n_ph = groundable_frac(r, args.window)
        item = r["meta"]["item"]
        by_item[item][1] += 1
        if frac >= args.threshold:
            by_item[item][0] += 1
            fam_kept["contract" if item in CONTRACT else "news"] += 1
            sum_g += frac
            # cap the stored input to the training window (delex caps anyway; keeps file small)
            rr = dict(r); rr["input"] = r["input"][:args.window]
            rr["meta"] = {**r["meta"], "groundable_frac": round(frac, 3)}
            kept.append(rr)
        if (i + 1) % 250 == 0:
            print(f"  scanned {i+1}/{len(rows)}  kept {len(kept)}", flush=True)

    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as fh:
        for r in kept:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nKEPT {len(kept)}/{len(rows)} ({100*len(kept)//len(rows)}%)  -> {outp}")
    print(f"  contract-family kept: {fam_kept['contract']}   news kept: {fam_kept['news']}")
    print(f"  kept-set mean groundability: {100*sum_g/len(kept):.1f}%" if kept else "  (none kept)")
    print("  by Item (kept/total):")
    for it in sorted(by_item, key=lambda x: -by_item[x][1]):
        k, t = by_item[it]
        print(f"    {it:5}: {k:4}/{t:<4} ({100*k//t if t else 0}%)")


if __name__ == "__main__":
    main()
