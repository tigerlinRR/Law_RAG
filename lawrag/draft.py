"""8-K drafting experiment: contract -> grounded Item disclosure draft.

Deliberately NOT a fine-tuned model. SEC disclosures are fact-critical and
zero-tolerance for hallucination, so drafting stays retrieval + extraction +
a base model instructed to use only the given facts:

  1. Extract structured facts from the source contract (reuses the existing
     due-diligence engine — same clause checklist, verbatim quotes).
  2. Retrieve prior 8-K filings of the SAME Item type as a structure/style
     reference only (never a source of facts).
  3. Ask the model to draft the Item disclosure using ONLY the extracted
     contract facts, following the precedents' structure/tone, with every
     fact traced back to its verbatim source quote.

If a fact the disclosure needs isn't in the contract, the model must say so
rather than invent it — the output is meant to be checked line-by-line
against the citations, not trusted as a finished filing.
"""
from __future__ import annotations

from pathlib import Path

from . import llm, retrieve
from .summarize import review_contract, verify_quote

ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.03": "Creation of a Direct Financial Obligation",
    "3.02": "Unregistered Sales of Equity Securities",
    "5.02": "Departure/Election of Directors or Officers",
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {"type": "string"},
        "item_title": {"type": "string"},
        "disclosure": {"type": "string"},
        "facts_used": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "source_quote": {"type": "string"},
                },
                "required": ["fact", "source_quote"],
            },
        },
    },
    "required": ["item", "item_title", "disclosure", "facts_used"],
}

_SYSTEM = (
    "You are a securities lawyer drafting an SEC Form 8-K disclosure. You are given "
    "(a) a contract summary for context plus a list of extracted clauses, each with "
    "a verbatim quote, and (b) prior 8-K filings of the SAME item type, provided "
    "ONLY as structural/style reference. Match the precedents' structure, tone, and "
    "level of detail. NEVER reuse a precedent's facts (names, dates, amounts, "
    "counterparties) — every fact in your draft must come from the source contract "
    "facts provided. If a fact the disclosure normally needs is missing from the "
    "contract facts, write '[NOT STATED IN CONTRACT]' instead of inventing it. For "
    "every factual sentence in your draft, add one entry to 'facts_used': its "
    "'source_quote' MUST be copied character-for-character from one of the listed "
    "clause quotes (never from the contract-summary sentence, which is paraphrased "
    "context, not a quotable source — if a sentence combines several clauses, add "
    "one facts_used entry per clause quote it draws on)."
)


def _facts_block(review: dict) -> str:
    lines = [
        f"Parties: {', '.join(review.get('parties', [])) or '[unknown]'}",
        f"Contract summary: {review.get('summary', '')}",
    ]
    for c in review.get("clauses", []):
        if c.get("value", "").strip().lower() not in ("", "not found"):
            lines.append(f"- {c['name']}: {c['value']} (quote: \"{c['quote']}\")")
    return "\n".join(lines)


def _user_prompt(item: str, item_title: str, review: dict, precedents: list[str]) -> str:
    precedents_block = "\n\n---\n\n".join(precedents) if precedents else \
        "(no prior filing of this Item type found in the library — draft from the " \
        "contract facts alone, using standard 8-K disclosure conventions)"
    return (
        f"=== TARGET: Item {item} — {item_title} ===\n\n"
        f"=== FACTS EXTRACTED FROM THE SOURCE CONTRACT ===\n{_facts_block(review)}\n\n"
        f"=== PRIOR ITEM {item} FILINGS (structure/style reference ONLY — do not "
        f"reuse their facts) ===\n{precedents_block}"
    )


def draft_8k(
    contract_path: str | Path,
    item: str = "1.01",
    n_precedents: int = 2,
    allowed_clients: list[str] | None = None,
    exclude_document_ids: list[int] | None = None,
) -> dict:
    """Draft an 8-K Item disclosure for `contract_path`, grounded in facts extracted
    from that contract, using same-Item historical filings as a style reference only.

    `exclude_document_ids`: for held-out evaluation — exclude the real 8-K that this
    contract actually produced, so the "precedent" can't leak the answer."""
    item_title = ITEM_TITLES.get(item, "")
    review = review_contract(contract_path)

    hits = retrieve.search(
        f"8-K Item {item} {item_title}",
        filters=retrieve.Filters(doc_type="8-K"),
        top_k=n_precedents * 4,  # a few chunks per doc; grouped back into docs below
        allowed_clients=allowed_clients,
        meta_filters={"filing_items": item},
        exclude_document_ids=exclude_document_ids,
        use_rerank=False,  # precedent lookup is exact-match by item; RRF order is fine
    )
    by_doc: dict[int, list] = {}
    for h in hits:
        by_doc.setdefault(h.document_id, []).append(h)
    precedent_docs = list(by_doc.values())[:n_precedents]
    precedent_texts = ["\n".join(c.content for c in chs) for chs in precedent_docs]
    precedent_citations = [chs[0].citation() for chs in precedent_docs]

    result = llm.chat_json(
        _SYSTEM, _user_prompt(item, item_title, review, precedent_texts),
        DRAFT_SCHEMA, max_tokens=4096,
    )
    # Item/title are known inputs, not model output — set them deterministically
    # rather than trust free-form generation (which sometimes echoes precedent text).
    result["item"] = item
    result["item_title"] = item_title
    full_text = review.get("_full_text", "")
    for f in result.get("facts_used", []):
        f["verified"] = verify_quote(f.get("source_quote", ""), full_text)
    result["_source_contract"] = Path(contract_path).name
    result["_doc_type"] = review.get("doc_type", "")
    result["_precedents_used"] = precedent_citations
    result["_contract_summary"] = review.get("summary", "")
    return result
