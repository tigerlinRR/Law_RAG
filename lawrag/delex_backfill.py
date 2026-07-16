"""Backfill for the v4 delexicalized adapter.

v4 is trained on delexicalized 8-K pairs: deal-specific facts are masked with typed
indexed placeholders ([AMOUNT_n], [NUM_n], [PCT_n], [DATE_n], [ORG_n], [PERSON_n]) so the
model learns 8-K structure/tone/materiality but never a real value — it emits placeholders,
so it CANNOT fabricate a figure. At inference we:

  1. delex the incoming SOURCE doc with the SAME logic as training (`delex_source`),
     producing a {placeholder -> real surface value} map;
  2. feed the delexed source to v4 -> a delexed skeleton (same placeholders);
  3. `backfill` the skeleton with the map -> real values;
  4. any placeholder v4 emits that is NOT in the source map (hallucinated / off-by-one) is
     neutralized to a confirm marker and returned as `missing` -> the guardrail RED-flags it.

Masking here MUST be identical to training, so we reuse `training/llamafactory/delex.py`
directly (its regex + spaCy PERSON logic).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Reuse the EXACT training delex logic (regex table, ORG suffix, span collection, stoplist).
_DELEX_DIR = Path(__file__).resolve().parent.parent / "training" / "llamafactory"
if str(_DELEX_DIR) not in sys.path:
    sys.path.insert(0, str(_DELEX_DIR))
import delex as _dx  # noqa: E402  (loads spaCy at import — required, identical to training)

PLACEHOLDER_RE = re.compile(r"\[(?:AMOUNT|NUM|PCT|DATE|ORG|PERSON)_\d+\]")
CONFIRM_MARKER = "[NOT IN SOURCE — CONFIRM]"
SYSTEM = _dx.SYSTEM               # the exact system prompt v4 was trained with
SOURCE_WINDOW = _dx.MAX_INPUT     # chars of source fed to the adapter (MUST match training's max_input)


def delex_source(text: str) -> tuple[str, dict[str, str]]:
    """Delex `text` exactly as training does; return (delexed_text, {placeholder: surface}).

    `surface` is the real value to backfill — the original text of the occurrence that first
    claimed each placeholder (mirrors `delex.run`'s numbering: spans processed in descending
    start position, numbered on first encounter of a (type, canonical-value) pair)."""
    reg: dict[tuple, str] = {}
    cnt: dict[str, int] = {}
    surface: dict[str, str] = {}

    def ph(typ: str, can: str, surf: str) -> str:
        k = (typ, can)
        if k not in reg:
            cnt[typ] = cnt.get(typ, 0) + 1
            reg[k] = f"[{typ}_{cnt[typ]}]"
            surface[reg[k]] = surf
        return reg[k]

    out = text
    cores: dict[str, str] = {}
    for s, e, typ, can in sorted(_dx.collect(text), key=lambda x: -x[0]):
        if typ == "ORG":
            cores.setdefault(can, text[s:e])
        out = out[:s] + ph(typ, can, text[s:e]) + out[e:]
    # bare-name second pass: mask standalone core names (e.g. "SoundHound AI" without suffix)
    for can, surf in cores.items():
        core = re.sub(rf"\s+{_dx.CORP}\.?$", "", surf).strip()
        if len(core) >= 4:
            p = ph("ORG", can, surf)
            out = re.sub(r"\b" + re.escape(core) + r"\b", p, out)
    return out, surface


def backfill(skeleton: str, surface_map: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute [TYPE_n] placeholders in v4's `skeleton` back to real values from
    `surface_map`. A placeholder absent from the map (v4 hallucinated an index the source
    never had) is neutralized to CONFIRM_MARKER and collected in `missing` (-> guardrail
    RED). Returns (backfilled_text, missing_placeholders)."""
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        p = m.group(0)
        if p in surface_map:
            return surface_map[p]
        missing.append(p)
        return CONFIRM_MARKER

    return PLACEHOLDER_RE.sub(repl, skeleton), sorted(set(missing))
