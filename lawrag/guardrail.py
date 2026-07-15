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

    # count: bare comma-grouped numbers (>=4 digits, e.g. 10,000 or 1,724,418) not
    # attached to $ — catches smaller invented counts (e.g. a v4 literal "10,000 sq ft")
    # that a >=7-digit-only rule would miss.
    for m in re.finditer(r"(?<![\$\d.])\d{1,3}(?:,\d{3})+\b", text):
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


def _match_derived(d: Datum, derived: list | None) -> str | None:
    """Return the derivation description if numeric `d` matches a caller-supplied,
    semantically-anchored derived value (e.g. share count = aggregate ÷ per-share price,
    computed ONCE in draft.py from the labeled clauses), else None.

    We deliberately do NOT blind-search all source-number pairs for a product/quotient:
    with many figures present that yields COINCIDENTAL matches (a wrong 1,000,000 =
    $50,000 × $20) and would wrongly ground a fabricated number. Only the specific,
    labeled computation passed in is honored — a wrong figure stays fabricated (RED)."""
    if not derived or d.kind not in ("currency", "count"):
        return None
    for value, desc in derived:
        tol = abs(value) * Decimal("0.005") + max(d.step, Decimal(1)) / 2
        if abs(d.canonical - value) <= tol:
            return desc
    return None


def _derive_from_grounded(d: Datum, grounded: list[Datum]) -> str | None:
    """Derive `d` from two figures that are BOTH source-grounded AND stated in the draft
    (`grounded`). Lower-coincidence and transparent than a blind source-pair search: the
    operands are visible in the draft, so the shown '= A ÷ B' is self-checking. Lets a
    human-corrected draft (e.g. edited to '8,500,000 shares … $4.55 … $38,675,000') pass
    once the count equals an arithmetic combination of the figures the draft itself uses."""
    if d.kind not in ("currency", "count"):
        return None
    tol = abs(d.canonical) * Decimal("0.005") + max(d.step, Decimal(1)) / 2
    nums = [x for x in grounded if x.canonical != 0 and x is not d]
    for a in nums:
        for b in nums:
            if a is b or b.canonical == 0:
                continue
            if abs(a.canonical) >= 1000 and abs(d.canonical - a.canonical / b.canonical) <= tol:
                return f"= {a.raw} ÷ {b.raw}"
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            if abs(d.canonical) >= 1000 and abs(d.canonical - a.canonical * b.canonical) <= tol:
                return f"= {a.raw} × {b.raw}"
    return None


# --- public API ---------------------------------------------------------------
def reconcile(draft_text: str, source_text: str,
              must_disclose: set[str] | None = None,
              derived: list | None = None) -> dict:
    """Reconcile every material datum in the DRAFT against the SOURCE.

    Returns {"verdict": clean|needs_review|blocked, "items": [...]} where each item is
    {raw, normalized, kind, status ∈ {matched,derived,fabricated,omitted}, source_snippet}.

    - fabricated (draft datum absent from source AND not an arithmetic consequence of it)
      -> RED. **Only a fabrication blocks** (spec §4, amended 2026-07-10).
    - derived (draft figure = an exact product/quotient of two verbatim source figures,
      e.g. share count = aggregate ÷ per-share price) -> review-required, NON-blocking;
      `source_snippet` shows the arithmetic so a human confirms it (and catches a wrong
      derivation). Grounded in the source, just not written verbatim.
    - omitted (source datum absent from draft) -> AMBER, REVIEW-ONLY, never blocks and
      never affects the verdict. A blanket omission check drowns RED in noise against
      8-K's deliberately selective disclosure (39 AMBER in the field test), so omissions
      are emitted ONLY for rubric MUST-disclose fields: pass `must_disclose` = context
      keywords for the Item's required fields (e.g. 1.01 assumed debt -> {"indebtedness",
      "debt"}). With none supplied we ship RED-only (Option A) -- safe on its own; lawyer
      sign-off backstops materiality. Scoped AMBER (Option B) turns on when the
      rubric->keyword mapping is wired.
    """
    d_data = extract(draft_text)
    s_data = extract(source_text)
    source_low = source_text.lower()
    items: list[dict] = []
    blocked = review = False

    # Pass 1: classify direct source matches (so derivation can use grounded draft figures).
    classified = []  # (datum, matched, snippet)
    for d in d_data:
        hit = _find(d, s_data)
        matched = hit is not None
        if not matched and d.kind == "party" and _party_in_source_text(d, source_low):
            matched = True
        classified.append((d, matched, hit.ctx if hit else None))
    grounded_nums = [d for d, m, _ in classified if m and d.kind in ("currency", "count")]

    # Pass 2: unmatched figures may still be grounded by an arithmetic derivation.
    for d, matched, snippet in classified:
        if matched:
            status = "matched"
        else:
            deriv = _match_derived(d, derived) or _derive_from_grounded(d, grounded_nums)
            if deriv:
                status, snippet, review = "derived", deriv, True
            else:
                status, blocked = "fabricated", True
        items.append({"raw": d.raw, "normalized": str(d.canonical), "kind": d.kind,
                      "status": status, "source_snippet": snippet})

    if must_disclose:
        kws = [k.lower() for k in must_disclose]
        reported: set[tuple] = set()
        for s in s_data:
            if _find(s, d_data):
                continue
            if not any(k in (s.ctx or "").lower() for k in kws):
                continue                  # not a rubric MUST-disclose field -> skip
            dedup = (s.kind, str(s.canonical))
            if dedup in reported:
                continue
            reported.add(dedup)
            review = True
            items.append({"raw": s.raw, "normalized": str(s.canonical), "kind": s.kind,
                          "status": "omitted", "source_snippet": s.ctx})

    # Only a true fabrication BLOCKS. Derived figures (and rubric omissions) are
    # review-required but non-blocking -> a human confirms; they never gate "ready".
    verdict = "blocked" if blocked else "needs_review" if review else "clean"
    return {"verdict": verdict, "items": items}
