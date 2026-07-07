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

from pathlib import Path

from . import llm
from .config import CONFIG
from .parsers import parse

# Standard due-diligence clause checklist for commercial contracts.
CHECKLIST = [
    "Parties", "Effective Date", "Term / Duration", "Termination",
    "Auto-Renewal", "Governing Law", "Confidentiality", "Indemnification",
    "Limitation of Liability", "Assignment / Change of Control",
    "Exclusivity / Non-Compete", "Payment Terms", "Intellectual Property",
    "Dispute Resolution",
]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string"},
        "summary": {"type": "string"},
        "parties": {"type": "array", "items": {"type": "string"}},
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["name", "value", "quote"],
            },
        },
        "key_risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_type", "summary", "parties", "clauses", "key_risks"],
}

_SYSTEM = (
    "You are a senior corporate attorney assisting with contract due diligence. "
    "Extract ONLY information explicitly present in the contract text. Never invent "
    "or assume terms. If a checklist clause is absent, set its value to 'Not found' "
    "and quote to an empty string. Every 'quote' MUST be copied verbatim from the "
    "text. Flag anything a reviewing lawyer should pay attention to (unusual terms, "
    "one-sided provisions, missing standard protections) in key_risks."
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
    merged["clauses"] = [by_clause[c] for c in checklist if c in by_clause] or \
        list(by_clause.values())
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

    if len(full) <= CONFIG.llm_max_ctx_chars:
        result = _extract_pass(full, checklist)
    else:
        # Split into overlapping windows and map-reduce.
        step = CONFIG.llm_max_ctx_chars
        windows = [full[i:i + step] for i in range(0, len(full), step)]
        result = _merge([_extract_pass(w, checklist) for w in windows], checklist)

    for cl in result.get("clauses", []):
        if cl.get("value", "").strip().lower() not in ("", "not found"):
            cl["verified"] = verify_quote(cl.get("quote", ""), full)

    result["_source"] = path.name
    result["_pages"] = max((b.page or 0 for b in blocks), default=0) or None
    result["_full_text"] = full
    return result
