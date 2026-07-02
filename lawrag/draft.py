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

import re
from pathlib import Path

from . import llm, retrieve
from .summarize import CHECKLIST as _DEFAULT_CHECKLIST
from .summarize import review_contract, verify_quote

ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.03": "Creation of a Direct Financial Obligation",
    "3.02": "Unregistered Sales of Equity Securities",
    "5.02": "Departure/Election of Directors or Officers",
}

# Per-Item extraction checklists — a financial instrument needs principal/
# interest/maturity, not services-contract terms like IP or exclusivity. Items
# not listed here fall back to the default general-commercial-contract
# checklist in summarize.py (fine for 1.01/1.02/2.01-style agreements).
ITEM_CHECKLISTS: dict[str, list[str]] = {
    "2.03": [
        "Parties (Lender/Investor and Borrower)", "Instrument Date",
        "Principal Amount", "Purchase Price / Original Issue Discount",
        "Interest Rate", "Maturity Date", "Payment / Repayment Terms",
        "Conversion Rights", "Redemption Rights",
        "Related Agreements Referenced", "Security / Collateral",
        "Default / Acceleration Provisions",
    ],
}


def _checklist_for(item: str) -> list[str]:
    return ITEM_CHECKLISTS.get(item, _DEFAULT_CHECKLIST)


# Defined terms that name a party/role, not the instrument being disclosed — the
# qualifier must reference the instrument ("the Note"), never the registrant.
_PARTY_TERMS = {
    "company", "investor", "holder", "vendor", "client", "purchaser", "seller",
    "borrower", "lender", "buyer", "supplier", "party", "parties", "sec",
    "registrant", "counterparty", "guarantor", "issuer", "lessor", "lessee",
}


def _ensure_exhibit_qualifier(disclosure: str) -> str:
    """Every real 8-K Item disclosure closes with the standard 'qualified in its
    entirety by reference to the full text... Exhibit 10.1' sentence. The model
    usually writes it but occasionally drops it, so guarantee it — it's fixed
    boilerplate, not a fact, and never needs a source citation."""
    if "qualified in its entirety" in disclosure.lower():
        return disclosure
    # Pick the instrument's defined term, e.g. (the "Note") / (the "Agreement") —
    # the FIRST defined term is usually a party ("the Company"), so skip party roles.
    terms = re.findall(r'\(the [“”"]([A-Z][A-Za-z ]+?)[“”"]\)', disclosure)
    noun = next((t for t in terms if t.strip().lower() not in _PARTY_TERMS), None) \
        or (terms[0] if terms else "Agreement")
    qualifier = (
        f"The foregoing description of the {noun} does not purport to be complete and "
        f"is qualified in its entirety by reference to the full text of such {noun}, a "
        f"copy of which is filed as Exhibit 10.1 to this Current Report on Form 8-K and "
        f"is incorporated herein by reference."
    )
    return disclosure.rstrip() + "\n\n" + qualifier

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
    "You are a securities lawyer drafting an SEC Form 8-K Item disclosure. A Form 8-K "
    "disclosure is a BRIEF, investor-facing description of the MATERIAL terms of a "
    "transaction — it is NOT a comprehensive summary of the contract. Real 8-K "
    "disclosures are typically one to three short paragraphs that state the nature of "
    "the transaction and only its most material commercial or economic terms, then "
    "defer everything else to the full agreement filed as an exhibit.\n\n"
    "You are given (a) facts extracted from the source contract, each with a verbatim "
    "quote, and (b) prior 8-K filings of the SAME Item type. The precedents are your "
    "model for HOW MUCH to include and WHICH KINDS of terms are material for this Item "
    "type — match their length and selectivity, not just their tone. If the precedents "
    "disclose only a handful of terms, you must be equally selective.\n\n"
    "RULES:\n"
    "1. Include only the material terms a reasonable investor needs: the nature of the "
    "transaction, the parties, the date, and the key commercial/economic terms the "
    "precedents show are material for this Item type. LEAVE OUT the rest.\n"
    "2. Do NOT describe standard or protective/boilerplate provisions individually "
    "(e.g. governing law, dispute resolution, confidentiality, indemnification, "
    "limitation of liability, standard IP assignment, representations and warranties, "
    "default/acceleration mechanics, ownership caps). If worth mentioning at all, "
    "collapse them into a brief catch-all such as 'and other customary provisions' — "
    "unless a specific provision is genuinely unusual AND material.\n"
    "3. Do NOT write a clause-by-clause list or a 'Key terms include: - X - Y' "
    "enumeration. Write flowing prose in the style of the precedents.\n"
    "4. End with the standard qualifier, adapted to the instrument: 'The foregoing "
    "description of the [Agreement/Note/etc.] does not purport to be complete and is "
    "qualified in its entirety by reference to the full text of such [agreement/note], "
    "a copy of which is filed as Exhibit 10.1 to this Current Report on Form 8-K and "
    "is incorporated herein by reference.'\n"
    "5. NEVER reuse a precedent's facts (names, dates, amounts, counterparties) — every "
    "fact must come from the source contract facts. If a term the disclosure would "
    "normally state is missing from the contract facts, write '[NOT STATED IN "
    "CONTRACT]' rather than inventing it.\n"
    "6. For each factual statement you DO disclose, add one entry to 'facts_used': "
    "'fact' is that statement AS WORDED IN YOUR DISCLOSURE (a short sentence or "
    "clause copied from what you wrote, NOT a category label like 'Parties' or "
    "'Interest Rate'), and 'source_quote' MUST be copied character-for-character from "
    "one of the listed clause quotes (never from the contract-summary sentence, which "
    "is paraphrased context). Cite only the facts you actually disclose — fewer, "
    "material facts is correct, not a shortcoming."
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
    review = review_contract(contract_path, checklist=_checklist_for(item))

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
    result["disclosure"] = _ensure_exhibit_qualifier(result.get("disclosure", ""))
    full_text = review.get("_full_text", "")
    for f in result.get("facts_used", []):
        f["verified"] = verify_quote(f.get("source_quote", ""), full_text)
    result["_source_contract"] = Path(contract_path).name
    result["_doc_type"] = review.get("doc_type", "")
    result["_precedents_used"] = precedent_citations
    result["_contract_summary"] = review.get("summary", "")
    # Full extraction (every checklist term found), so a reviewer can see what the
    # disclosure deliberately LEFT OUT — the disclosure is intentionally selective,
    # this keeps that selectivity transparent rather than hiding dropped terms.
    result["_all_extracted_terms"] = [
        {"name": c.get("name", ""), "value": c.get("value", "")}
        for c in review.get("clauses", [])
        if c.get("value", "").strip().lower() not in ("", "not found")
    ]
    return result
