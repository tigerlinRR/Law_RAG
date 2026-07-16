"""Tests for the v4 delex backfill (lawrag.delex_backfill). Run from the repo root:
    ./.venv/bin/python tests/test_delex_backfill.py
Needs spaCy (delex.py loads en_core_web_sm)."""
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lawrag import delex_backfill as bf  # noqa: E402


def _norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def test_source_roundtrip():
    """Delexing a source then backfilling with its own map reconstructs it — proving the
    placeholder map + substitution are consistent. The guarantee is VALUE/content-preserving
    (not byte-verbatim): lossless canon merges format variants of the same entity onto one
    placeholder, so a collided occurrence backfills to the first surface's form (e.g. a
    line-break inside a name normalizes to a space). Compared whitespace-normalized, so any
    changed digit/letter — a real corruption — still fails the assert."""
    rows = [json.loads(l) for l in
            gzip.open(ROOT / "training/dataset/train_pairs.jsonl.gz", "rt") if l.strip()]
    src = next(r["input"] for r in rows if "$" in r["input"])
    delexed, smap = bf.delex_source(src)
    assert bf.PLACEHOLDER_RE.search(delexed), "source should contain placeholders"
    assert smap, "should have produced a placeholder->surface map"
    rebuilt, missing = bf.backfill(delexed, smap)
    assert missing == [], f"no placeholder should be unmapped, got {missing}"
    assert _norm_ws(rebuilt) == _norm_ws(src), "backfill must reconstruct the source content"


def test_hallucinated_index_flagged():
    """A placeholder v4 emits that the source map never had (hallucinated / off-by-one)
    is neutralized to the CONFIRM marker and reported for the guardrail to RED."""
    out, missing = bf.backfill(
        "The Company sold [AMOUNT_1] and also [AMOUNT_99].", {"[AMOUNT_1]": "$5,000,000"})
    assert missing == ["[AMOUNT_99]"]
    assert "$5,000,000" in out and bf.CONFIRM_MARKER in out


if __name__ == "__main__":
    test_source_roundtrip()
    test_hallucinated_index_flagged()
    print("delex_backfill tests PASS")
