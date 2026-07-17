"""Due-diligence engine: summarize a contract and extract key clauses / risks.

Design choices tuned for reliability on a local model:
  * Structured output via guided JSON (see llm.chat_json) — no fragile text parsing.
  * A fixed clause CHECKLIST so every contract is reviewed consistently.
  * Verbatim `quote` per clause so a lawyer can trace each finding to the source.
  * Map-reduce for contracts that exceed a single context window, instead of relying
    on ever-larger context (more reliable and works within the local 32k window).
  * The model is instructed to NEVER invent terms — absent clauses are "Not found".
"""
from __future__ import annotations

import re
from pathlib import Path

from . import llm
from .config import CONFIG
from .parsers import parse

# Some Richtech exhibits are filed with portions redacted under Item 601(b)(10)(iv)
# -- the PDF text extraction then contains literal block/placeholder glyphs (e.g.
# a run of "█"). Those are source-formatting artifacts, not real fact content;
# collapse them to a single marker so extraction never hands the drafting step a
# wall of glyphs to unwittingly echo into a filing.
_REDACTION_RE = re.compile(r"[▀-▟■-◿]{2,}|\*{3,}|_{5,}")


def _scrub_redactions(value: str) -> str:
    if not value:
        return value
    cleaned = _REDACTION_RE.sub("[REDACTED]", value)
    return re.sub(r"(\[REDACTED\][ ,]*){2,}", "[REDACTED] ", cleaned).strip()

# Standard due-diligence clause checklist for commercial contracts.
CHECKLIST = [
    "Parties", "Effective Date", "Term / Duration", "Termination",
    "Auto-Renewal", "Governing Law", "Confidentiality", "Indemnification",
    "Limitation of Liability", "Assignment / Change of Control",
    "Exclusivity / Non-Compete", "Payment Terms", "Intellectual Property",
    "Dispute Resolution",
]

# NOTE: the string/array fields carry maxLength/maxItems bounds. These keep the
# structured-output generation from overrunning the context window: the 8-K style
# model is far more verbose than the base, and without bounds it writes essays into
# each field until it hits max_tokens mid-string -> truncated (invalid) JSON. Bounding
# each field keeps total output well under the room left after the (large) prompt.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "maxLength": 80},
        "summary": {"type": "string", "maxLength": 700},
        "parties": {"type": "array", "maxItems": 12,
                    "items": {"type": "string", "maxLength": 150}},
        "clauses": {
            # room for the fixed checklist PLUS open-ended "other material terms" (the
            # long-tail mechanism that lets one tool handle any contract type).
            "type": "array", "maxItems": 45,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 80},
                    "value": {"type": "string", "maxLength": 300},
                    "quote": {"type": "string", "maxLength": 500},
                },
                "required": ["name", "value", "quote"],
            },
        },
        "key_risks": {"type": "array", "maxItems": 15,
                      "items": {"type": "string", "maxLength": 300}},
    },
    "required": ["doc_type", "summary", "parties", "clauses", "key_risks"],
}

_SYSTEM = (
    "You are a senior corporate attorney assisting with contract due diligence. "
    "Extract ONLY information explicitly present in the contract text. Never invent "
    "or assume terms. If a checklist clause is absent, set its value to 'Not found' "
    "and quote to an empty string. Every 'quote' MUST be copied verbatim from the "
    "text. Flag anything a reviewing lawyer should pay attention to (unusual terms, "
    "one-sided provisions, missing standard protections) in key_risks. "
    "Be terse: each 'value' is a brief phrase (not a paragraph); each 'quote' is only "
    "the specific clause sentence(s), copied verbatim, never a whole section; 'summary' "
    "is 3-5 sentences. Do not restate or elaborate."
)


def verify_quote(quote: str, source_text: str) -> bool:
    """True if `quote` appears verbatim (whitespace-normalized) in `source_text`.

    Extraction is instructed to always quote verbatim, but on messy real-world
    documents (redactions, dense formatting) it occasionally paraphrases instead
    -- this catches that so callers can flag an unverified citation rather than
    silently trust it."""
    if not quote or not quote.strip():
        return False
    norm = lambda s: " ".join(s.split())
    return norm(quote) in norm(source_text)


def _user_prompt(text: str, checklist: list[str]) -> str:
    checklist_block = "\n".join(f"- {c}" for c in checklist)
    return (
        "Review the contract below. Produce one 'clauses' entry for EACH checklist "
        f"item, in this order:\n{checklist_block}\n\n"
        "THEN, after the checklist entries, add extra 'clauses' entries for ANY OTHER "
        "term specific to THIS agreement that a reasonable investor would consider "
        "material but that the checklist above does not cover — e.g. an exclusivity or "
        "sole-agent arrangement and its duration, a standstill, a right of first refusal, "
        "an unusual fee or commission, a liability cap, a most-favored-nation clause, an "
        "earn-out, a lock-up. Give each a short descriptive 'name', a brief 'value', and a "
        "verbatim 'quote'. Do NOT invent — include a term only if it is actually present in "
        "this contract. This open list is what lets the tool handle any contract type, not "
        "just the ones the checklist anticipates.\n\n"
        "Also write a concise plain-language 'summary' (3-5 sentences), list the "
        "'parties', and list 'key_risks'.\n\n"
        f"=== CONTRACT TEXT ===\n{text}"
    )


def _extract_pass(text: str, checklist: list[str]) -> dict:
    # One clause object (name/value/quote) per checklist item, so a longer
    # checklist (e.g. Item 1.01's 23-field one) needs proportionally more
    # output budget -- 4096 was tuned for the 14-item default checklist and
    # truncates mid-JSON on larger ones, especially with long verbatim quotes
    # (e.g. redacted contract text).
    max_tokens = min(12000, 4096 + 350 * max(0, len(checklist) - 14))
    return llm.chat_json(_SYSTEM, _user_prompt(text, checklist), REVIEW_SCHEMA,
                          max_tokens=max_tokens)


def _repair_extraction(full: str, result: dict, checklist: str) -> int:
    """Second, VERIFY-GATED extraction pass — the #1 quality lever.

    Targets clauses that are (a) present but whose quote did NOT verify (the model
    paraphrased instead of copying — the observed near-miss failure mode), or (b) still
    'Not found' (a first-pass miss). Asks the model to return the EXACT verbatim quote for
    just those fields, and accepts a repair ONLY if the new quote verifies against the
    source. So it improves fidelity AND completeness while NEVER inventing: an unverifiable
    answer is rejected, not written. Returns the count of repaired clauses."""
    by_name = {cl.get("name", ""): cl for cl in result.get("clauses", [])}
    targets: list[str] = []
    for name in checklist:
        cl = by_name.get(name)
        if cl is None or cl.get("value", "").strip().lower() in ("", "not found"):
            targets.append(name)                     # completeness: (re)try a missed field
        elif not cl.get("verified", False):
            targets.append(name)                     # fidelity: repair a drifted quote
    if not targets:
        return 0
    schema = {
        "type": "object",
        "properties": {"fields": {
            "type": "array", "maxItems": len(targets),
            "items": {"type": "object", "properties": {
                "name": {"type": "string", "maxLength": 80},
                "value": {"type": "string", "maxLength": 300},
                "quote": {"type": "string", "maxLength": 500},
            }, "required": ["name", "value", "quote"]}}},
        "required": ["fields"]}
    block = "\n".join(f"- {t}" for t in targets)
    user = (
        "For EACH field below, find the single sentence in the contract that states it and "
        "copy that sentence VERBATIM into 'quote' (exact characters, no paraphrase, no "
        "summarizing); put the concise extracted fact in 'value'. If a field is genuinely "
        "absent from the contract, set value to 'Not found' and quote to ''. Do NOT guess.\n\n"
        f"FIELDS:\n{block}\n\n=== CONTRACT TEXT ===\n{full}")
    try:
        out = llm.chat_json(_SYSTEM, user, schema,
                            max_tokens=min(12000, 2048 + 300 * len(targets)))
    except Exception:
        return 0
    repaired = 0
    for f in out.get("fields", []):
        name = f.get("name", "")
        newv = _scrub_redactions(f.get("value", "") or "")
        newq = f.get("quote", "") or ""
        if newv.strip().lower() in ("", "not found") or not verify_quote(newq, full):
            continue                                 # reject anything unverifiable
        cl = by_name.get(name)
        if cl is None:
            cl = {"name": name}
            result["clauses"].append(cl)
            by_name[name] = cl
        # only overwrite when this is a genuine improvement (was missing or unverified)
        if cl.get("value", "").strip().lower() in ("", "not found") or not cl.get("verified"):
            cl.update(value=newv, quote=newq, verified=True)
            repaired += 1
    return repaired


def _merge(partials: list[dict], checklist: list[str]) -> dict:
    """Reduce step for map-reduce: keep the first substantive value per clause."""
    merged = {"doc_type": "", "summary": "", "parties": [], "clauses": [], "key_risks": []}
    by_clause: dict[str, dict] = {}
    for p in partials:
        merged["doc_type"] = merged["doc_type"] or p.get("doc_type", "")
        for party in p.get("parties", []):
            if party not in merged["parties"]:
                merged["parties"].append(party)
        merged["key_risks"].extend(p.get("key_risks", []))
        for cl in p.get("clauses", []):
            name = cl.get("name", "")
            found = cl.get("value", "").strip().lower() not in ("", "not found")
            if name not in by_clause or (found and not
                    (by_clause[name]["value"].strip().lower() not in ("", "not found"))):
                by_clause[name] = cl
    # checklist clauses first (in order), THEN any open-ended "other material terms"
    # (names not in the checklist) so the long-tail extraction survives map-reduce too.
    ordered = [by_clause[c] for c in checklist if c in by_clause]
    extras = [cl for name, cl in by_clause.items() if name not in checklist]
    merged["clauses"] = (ordered + extras) or list(by_clause.values())
    # Summarize the concatenated per-part summaries into one.
    joined = " ".join(p.get("summary", "") for p in partials)
    merged["summary"] = llm.chat(
        "You condense text.", f"Summarize in 3-5 sentences:\n{joined}",
        max_tokens=512) if joined else ""
    return merged


def review_contract(path: str | Path, checklist: list[str] | None = None) -> dict:
    """Parse a PDF/Word contract and return a structured due-diligence review.

    `checklist` overrides the default general-commercial-contract CHECKLIST —
    e.g. a financial-instrument checklist (principal/interest/maturity) for
    documents that don't look like a services agreement."""
    checklist = checklist or CHECKLIST
    path = Path(path)
    blocks = parse(path)
    full = "\n\n".join(b.text for b in blocks)

    single_window = len(full) <= CONFIG.llm_max_ctx_chars
    if single_window:
        result = _extract_pass(full, checklist)
    else:
        # Split into OVERLAPPING windows and map-reduce (overlap avoids missing a fact
        # that straddles a window boundary).
        step = CONFIG.llm_max_ctx_chars
        overlap = min(2000, step // 5)
        windows = [full[i:i + step] for i in range(0, len(full), max(1, step - overlap))]
        result = _merge([_extract_pass(w, checklist) for w in windows], checklist)

    result["parties"] = [_scrub_redactions(p) for p in result.get("parties", [])]
    result["summary"] = _scrub_redactions(result.get("summary", ""))
    for cl in result.get("clauses", []):
        # Scrub the VALUE only -- `quote` must stay byte-for-byte from the source
        # for verify_quote()/the review-pack audit trail, redaction glyphs and all.
        cl["value"] = _scrub_redactions(cl.get("value", ""))
        if cl.get("value", "").strip().lower() not in ("", "not found"):
            cl["verified"] = verify_quote(cl.get("quote", ""), full)

    # #1 quality lever: verify-gated repair pass (fixes drifted quotes + retries misses).
    # Only when the whole doc fits one window, so the repair sees the full source at once.
    if single_window:
        result["_repaired"] = _repair_extraction(full, result, checklist)

    result["_source"] = path.name
    result["_pages"] = max((b.page or 0 for b in blocks), default=0) or None
    result["_full_text"] = full
    return result
