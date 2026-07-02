"""Auto-extract library metadata from a document's text using the local LLM.

At scale nobody will hand-tag thousands of files, so at ingest time we read the
top of each document and let the model identify its type, title, parties, likely
client, and date. Extracted values fill only the fields the caller didn't set
explicitly, and are marked auto=True so they can be surfaced for lawyer review.
"""
from __future__ import annotations

import re

from .llm import chat_json

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string"},
        "title": {"type": "string"},
        "parties": {"type": "array", "items": {"type": "string"}},
        "client": {"type": "string"},
        "doc_date": {"type": "string"},
        "filing_item": {"type": "string"},
    },
    "required": ["doc_type", "title", "parties", "client", "doc_date", "filing_item"],
}

_SYSTEM = (
    "You classify legal documents for a law firm's internal library. From the text, "
    "identify: doc_type (a short canonical label such as 'NDA', 'S-8', '8-K', 'Master "
    "Services Agreement', 'Employment Agreement', 'Equity Incentive Plan', 'Board "
    "Resolution', 'Memo'); title (a short human-readable title); parties (the named "
    "entities/parties); client (the primary entity the document concerns — empty "
    "string if unclear); doc_date (the document's own date as YYYY-MM-DD, empty "
    "string if none); filing_item (ONLY for SEC Form 8-K filings: the disclosed Item "
    "number, e.g. '1.01' or '5.02', taken from an 'Item X.XX' heading in the text — "
    "empty string for every other document type or if no Item heading is present). "
    "Use ONLY information present in the text. Never invent."
)


def extract_metadata(text: str, filename: str, max_chars: int = 8000) -> dict:
    """Return {doc_type, title, parties, client, doc_date} inferred from the text.

    Only the top `max_chars` are used — type/parties/date live near the top and this
    keeps extraction fast and cheap."""
    user = f"Filename: {filename}\n\n=== DOCUMENT (beginning) ===\n{text[:max_chars]}"
    md = chat_json(_SYSTEM, user, METADATA_SCHEMA)
    date = (md.get("doc_date") or "").strip()
    md["doc_date"] = date if _DATE_RE.match(date) else ""
    return md
