"""8-K fact-reconciliation guardrail. Implements training/GUARDRAIL_SPEC.md.

Reconciles every *material datum* in a DRAFT 8-K disclosure against the SOURCE
document it was generated from -- catching fabricated figures (RED, block) and
material omissions (AMBER, review) that presence-only checks (draft._compliance_flags)
miss. The style adapter is style-only and provably fabricates figures; this layer is
the grounding / fact-fidelity net that runs AFTER drafting and BEFORE a human sees it.

Pure local text processing: NO DB, NO embedding, NO retrieval. Orthogonal to the
vector stack -- cutting pgvector/embed/rerank does not cut this ("no RAG" != "no
fact-check"). It never auto-files and does not replace lawyer sign-off.

Kinds reconciled (spec Sec.2): currency, count (shares/units), percent, date, party.
Out of scope (never treated as facts): section ids (Item 1.01), rule/statute cites
(Rule 3b-7, Section 18/409A), form names (Form 8-K, S-3), boilerplate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .clients import normalize_key

# --- identifier spans that must NOT be mined for facts (spec Sec.2) ------------
_IDENTIFIER_RE = re.compile(
    r"""(?ix)
    \b(?:item|rule|section|form|article|regulation|exhibit|paragraph|schedule|annex)
      \s+ [\w().\-/]+          # Item 1.01, Rule 3b-7, Section 4(a)(2), Exhibit 10.1
    | \bform\s+[SF]-\d+\b       # Form S-3, Form F-1
    | \b[SF]-\d+\b              # bare S-3
    | \bact\s+of\s+\d{4}\b      # "...Act of 1934" (statute year, not an event date)
    """,
    re.VERBOSE,
)

_MAGNITUDE = {"thousand": Decimal(1_000), "million": Decimal(1_000_000),
              "billion": Decimal(1_000_000_000)}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

# role/defined-term words that are NOT entity names (skip as parties)
_ROLE_TERMS = {
    "company", "seller", "buyer", "purchaser", "client", "borrower", "lender",
    "issuer", "holder", "agent", "guarantor", "registrant", "parties", "party",
    "board", "committee", "plan", "agreement", "note", "closing", "effective",
    "exchange act", "securities act", "the company",
}
_ENTITY_SUFFIX = (r"Inc|Incorporated|LLC|L\.L\.C|Corp|Corporation|Ltd|Limited|"
                  r"Co|LP|L\.P|N\.A|Bank|Partners|Holdings|Group|Trust|Fund")


@dataclass
class Datum:
    raw: str
    kind: str                       # currency | count | percent | date | party
    canonical: object               # Decimal | str (ISO date | normalized name)
    step: Decimal = Decimal(0)      # rounding granularity (numeric kinds only)
    ctx: str = ""                   # surrounding snippet, for the source_snippet field


# --- numeric normalization ----------------------------------------------------
def _decimals(numeric: str) -> int:
    return len(numeric.split(".")[1]) if "." in numeric else 0


def _num(numeric: str) -> Decimal:
    return Decimal(numeric.replace(",", ""))


def _currency_and_count(text: str) -> list[Datum]:
    out: list[Datum] = []
    # currency: $ amount, optional magnitude word (allow line-break before magnitude)
    for m in re.finditer(
        r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand)?", text, re.I):
        numeric, mag = m.group(1), (m.group(2) or "").lower()
        try:
            val = _num(numeric)
        except InvalidOperation:
            continue
        if mag:
            val *= _MAGNITUDE[mag]
            step = _MAGNITUDE[mag] * (Decimal(10) ** -_decimals(numeric))
        else:
            step = Decimal(10) ** -_decimals(numeric)
        out.append(Datum(m.group(0).strip().rstrip(","), "currency", val, step,
                         _snippet(text, m)))

    # count: "<n> [million|billion] shares/units/interests", or "<n> million shares"
    for m in re.finditer(
        r"(\d[\d,]*(?:\.\d+)?)\s*(million|billion)?\s*"
        r"(shares|units|membership interests)", text, re.I):
        numeric, mag = m.group(1), (m.group(2) or "").lower()
        try:
            val = _num(numeric)
        except InvalidOperation:
            continue
        step = Decimal(1)
        if mag:
            val *= _MAGNITUDE[mag]
            step = _MAGNITUDE[mag] * (Decimal(10) ** -_decimals(numeric))
        out.append(Datum(m.group(0).strip().rstrip(","), "count", val, step,
                         _snippet(text, m)))

    # count: large bare comma-grouped numbers (>=7 digits) not attached to $
    for m in re.finditer(r"(?<![\$\d.])\d{1,3}(?:,\d{3}){2,}\b", text):
        start = m.start()
        if start > 0 and text[start - 1] == "$":
            continue
        out.append(Datum(m.group(0), "count", _num(m.group(0)), Decimal(1),
                         _snippet(text, m)))

    # count: bare "<n> million/billion" (no unit) in an equity/share context nearby
    for m in re.finditer(r"\b(\d[\d,]*(?:\.\d+)?)\s+(million|billion)\b", text, re.I):
        if text[max(0, m.start() - 1):m.start()] == "$":
            continue
        window = text[m.start():m.start() + 40].lower()
        if "share" not in window:
            continue
        numeric, mag = m.group(1), m.group(2).lower()
        val = _num(numeric) * _MAGNITUDE[mag]
        step = _MAGNITUDE[mag] * (Decimal(10) ** -_decimals(numeric))
        out.append(Datum(m.group(0).strip(), "count", val, step, _snippet(text, m)))
    return out


def _percents(text: str) -> list[Datum]:
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", text, re.I):
        numeric = m.group(1)
        val = _num(numeric) / Decimal(100)
        step = (Decimal(10) ** -_decimals(numeric)) / Decimal(100)
        out.append(Datum(m.group(0).strip(), "percent", val, step, _snippet(text, m)))
    return out


def _dates(text: str) -> list[Datum]:
    out = []
    # Month DD[th], YYYY
    for m in re.finditer(
        r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            continue
        iso = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
        out.append(Datum(m.group(0), "date", iso, ctx=_snippet(text, m)))
    # ISO YYYY-MM-DD
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        out.append(Datum(m.group(0), "date", iso, ctx=_snippet(text, m)))
    return out


def _parties(text: str) -> list[Datum]:
    out = []
    pat = re.compile(
        rf"\b([A-Z][A-Za-z0-9&.\'-]*(?:\s+[A-Z][A-Za-z0-9&.\'-]*)*"
        rf"[,]?\s+(?:{_ENTITY_SUFFIX})\b\.?)")
    seen = set()
    for m in pat.finditer(text):
        raw = m.group(1).strip().rstrip(".,")
        key = normalize_key(raw)
        if not key or key in _ROLE_TERMS or key in seen:
            continue
        # need a real name token beyond the bare suffix
        if len(key.split()) < 1 or raw.lower() in _ROLE_TERMS:
            continue
        seen.add(key)
        out.append(Datum(raw, "party", key, ctx=_snippet(text, m)))
    return out


def _snippet(text: str, m: re.Match, pad: int = 40) -> str:
    a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return " ".join(text[a:b].split())


def _mask_identifiers(text: str) -> str:
    return _IDENTIFIER_RE.sub(lambda x: " " * len(x.group(0)), text)


def extract(text: str) -> list[Datum]:
    """Mine the material data of every in-scope kind from `text`."""
    clean = _mask_identifiers(text)
    return (_currency_and_count(clean) + _percents(clean)
            + _dates(clean) + _parties(clean))


# --- matching -----------------------------------------------------------------
def _values_match(a: Datum, b: Datum) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind in ("currency", "count", "percent"):
        tol = max(a.step, b.step) / 2
        return abs(a.canonical - b.canonical) <= tol
    if a.kind == "date":
        return a.canonical == b.canonical
    if a.kind == "party":
        ka, kb = str(a.canonical), str(b.canonical)
        if ka == kb:
            return True
        ta, tb = set(ka.split()), set(kb.split())
        return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)
    return False


def _find(d: Datum, pool: list[Datum]) -> Datum | None:
    return next((p for p in pool if _values_match(d, p)), None)


def _party_in_source_text(d: Datum, source_low: str) -> bool:
    """Lenient party fallback: an entity named in the source prose without a
    corporate suffix (so not mined as a source `party`) still counts as grounded.
    Only downgrades a would-be fabrication to matched; never creates a new flag."""
    toks = str(d.canonical).split()
    return bool(toks) and all(t in source_low for t in toks)


# --- public API ---------------------------------------------------------------
def reconcile(draft_text: str, source_text: str) -> dict:
    """Reconcile every material datum in the DRAFT against the SOURCE.

    Returns {"verdict": clean|needs_review|blocked, "items": [...]} where each item
    is {raw, normalized, kind, status ∈ {matched,fabricated,omitted}, source_snippet}.
    - fabricated (draft datum absent from source)  -> RED, blocks "ready"
    - omitted   (source datum, of a kind the draft carries, absent from draft) -> AMBER
    """
    d_data = extract(draft_text)
    s_data = extract(source_text)
    source_low = source_text.lower()
    items: list[dict] = []
    blocked = review = False

    for d in d_data:
        hit = _find(d, s_data)
        matched = hit is not None
        if not matched and d.kind == "party" and _party_in_source_text(d, source_low):
            matched = True
        status = "matched" if matched else "fabricated"
        if not matched:
            blocked = True
        items.append({"raw": d.raw, "normalized": str(d.canonical), "kind": d.kind,
                      "status": status,
                      "source_snippet": hit.ctx if hit else None})

    draft_kinds = {d.kind for d in d_data}
    reported: set[tuple] = set()
    for s in s_data:
        if s.kind not in draft_kinds:
            continue                      # disclosure doesn't carry this kind of datum
        if _find(s, d_data):
            continue
        dedup = (s.kind, str(s.canonical))
        if dedup in reported:
            continue
        reported.add(dedup)
        review = True
        items.append({"raw": s.raw, "normalized": str(s.canonical), "kind": s.kind,
                      "status": "omitted", "source_snippet": s.ctx})

    verdict = "blocked" if blocked else "needs_review" if review else "clean"
    return {"verdict": verdict, "items": items}
