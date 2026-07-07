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

# Per-Item extraction checklists — each 8-K Item type discloses different facts,
# so the due-diligence extraction is pointed at what THAT Item needs (a note
# needs principal/interest/maturity; an equity sale needs securities/exemption).
# Only Items driven by a source transactional DOCUMENT are listed — event-driven
# Items with no underlying contract to draft from (bankruptcy, earnings,
# delisting notices, auditor changes, vote results, Reg FD/other events) are
# deliberately omitted; this tool drafts a disclosure FROM a document. Items not
# listed fall back to the default general-commercial-contract checklist.
ITEM_CHECKLISTS: dict[str, list[str]] = {
    "1.01": [  # Entry into a Material Definitive Agreement
        # Item 1.01 spans many deal types (real estate, financing, services,
        # securities), so it needs a broader extraction than the default
        # services-contract checklist -- notably asset description/size and
        # deposit/earnest money, which the default checklist lacks and which
        # the materiality-rubric analysis (see ITEM_RULES) showed are almost
        # always disclosed when present.
        "Parties", "Effective Date / Signing Date", "Nature of Transaction",
        "Asset(s) Involved (description, size, location, quantity)",
        "Purchase Price / Consideration", "Deposit / Earnest Money",
        "Financing Amount / Principal", "Interest Rate / Discount",
        "Maturity / Term / Duration", "Payment / Repayment Terms",
        "Closing / Completion Conditions", "Closing / Completion Timing",
        "Termination Rights", "Exclusivity / Non-Compete",
        "Conversion / Exchange Terms", "Redemption Rights",
        "Governing Law", "Confidentiality", "Indemnification",
        "Limitation of Liability", "Representations and Warranties",
        "Assignment / Change of Control", "Dispute Resolution",
    ],
    "1.02": [  # Termination of a Material Definitive Agreement
        "Parties", "Agreement Being Terminated (title and date)",
        "Termination Date", "Reason / Trigger for Termination",
        "Material Terms of Termination", "Termination Fees or Penalties",
        "Surviving Obligations", "Related Agreements Referenced",
    ],
    "2.01": [  # Completion of Acquisition or Disposition of Assets
        "Parties (Acquirer and Seller)", "Acquisition or Disposition",
        "Assets or Business Involved", "Closing / Completion Date",
        "Consideration / Purchase Price", "Form of Consideration (cash/stock/other)",
        "Material Terms and Conditions", "Related Agreements Referenced",
    ],
    "2.03": [  # Creation of a Direct Financial Obligation
        "Parties (Lender/Investor and Borrower)", "Instrument Date",
        "Principal Amount", "Purchase Price / Original Issue Discount",
        "Interest Rate", "Maturity Date", "Payment / Repayment Terms",
        "Conversion Rights", "Redemption Rights",
        "Related Agreements Referenced", "Security / Collateral",
        "Default / Acceleration Provisions",
    ],
    "3.02": [  # Unregistered Sales of Equity Securities
        "Issuer", "Purchaser(s) / Investor(s)",
        "Securities Sold (type and class)", "Date of Sale",
        "Number of Shares or Units", "Consideration / Price per Security",
        "Exemption Relied Upon", "Conversion or Exercise Terms",
        "Use of Proceeds", "Related Agreements Referenced",
    ],
    "5.02": [  # Departure/Election of Directors or Officers
        "Individual Name", "Position / Title",
        "Nature of Event (appointment/departure/resignation)",
        "Effective Date", "Reason (if departure)",
        "Compensatory Arrangement / Terms", "Agreement or Plan Type",
        "Related Agreements Referenced",
    ],
}


def _checklist_for(item: str) -> list[str]:
    return ITEM_CHECKLISTS.get(item, _DEFAULT_CHECKLIST)


# Defined terms that name a party/role, not the instrument being disclosed — the
# qualifier must reference the instrument ("the Note"), never the registrant.
# Safety net for rule 8 in _SYSTEM: catches the model still leaking the literal
# "[REDACTED]" marker or raw block/placeholder glyphs into a finished disclosure.
_HAS_REDACTION_LEAK = re.compile(r"\[REDACTED\]|[▀-▟■-◿]{2,}|\*{3,}")

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


def _ensure_material_relationship(disclosure: str) -> str:
    """Item 1.01(c) requires a statement of any material relationship between the
    registrant and a party OTHER THAN the agreement. The model usually writes it
    but drops it when the precedents don't model it, so guarantee it in standard
    form. It cannot be verified from the contract, so counsel must confirm it —
    but omitting the required (c) element entirely is worse than including the
    standard negative statement for review."""
    if "material relationship" in disclosure.lower():
        return disclosure
    terms = re.findall(r'\(the [“”"]([A-Z][A-Za-z ]+?)[“”"]\)', disclosure)
    instrument = next((t for t in terms if t.strip().lower() not in _PARTY_TERMS), None) \
        or (terms[0] if terms else "Agreement")
    counterparty = next(
        (t for t in terms if t.strip().lower() in _PARTY_TERMS
         and t.strip().lower() not in {"company", "registrant", "party", "parties"}),
        None)
    cp = f"the {counterparty}" if counterparty else "the counterparty"
    stmt = (f"Other than in respect of the {instrument}, there is no material "
            f"relationship between the Company and {cp}.")
    # Insert before the closing "qualified in its entirety" sentence if present.
    low = disclosure.lower()
    idx = low.find("the foregoing description")
    if idx != -1:
        return disclosure[:idx].rstrip() + "\n\n" + stmt + "\n\n" + disclosure[idx:]
    return disclosure.rstrip() + "\n\n" + stmt

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
    "quote, (b) prior 8-K filings of the SAME Item type, and (c) the mandatory SEC "
    "disclosure requirements for this Item. The precedents are your model for HOW MUCH "
    "to include and WHICH KINDS of terms are material — match their length and "
    "selectivity, not just their tone. Your draft MUST satisfy every mandatory SEC "
    "requirement in (c).\n\n"
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
    "material facts is correct, not a shortcoming.\n"
    "7. TONE: neutral and factual, written for the general investing public. Do NOT "
    "use promotional or puffery language (no 'exciting', 'leading', 'transformative', "
    "etc.). State the facts plainly.\n"
    "8. If an extracted fact's value contains the marker '[REDACTED]', the source "
    "contract itself redacts that information (e.g. under Item 601(b)(10)(iv) of "
    "Regulation S-K). NEVER write '[REDACTED]' or any placeholder characters into the "
    "disclosure, and never guess what was redacted. Instead refer to that party or "
    "term only by a short generic role-based description, exactly as Richtech's own "
    "real filings do — e.g. 'with one of the largest retailers in the world (the "
    "\"Client\")' rather than naming a redacted counterparty. If no natural generic "
    "description is available from the other facts, use a bare defined term like "
    "'a third party (the \"Client\")' with no further description."
)

# Mandatory SEC disclosure requirements per Item, from Richtech counsel's guidance
# (Form 8-K rules + the materiality standard from TSC Industries / Basic v. Levinson)
# PLUS a data-derived materiality rubric: which terms Richtech's own counsel has
# actually chosen to disclose vs. omit, measured by comparing all 17 of Richtech's
# real Item-1.01 filings against their underlying contracts (see
# law-rag-project-plan memory, "materiality rubric" entry, for the full breakdown).
# This is what counsel's judgment looks like in practice, not just the legal text.
ITEM_RULES: dict[str, str] = {
    "1.01": (
        "SEC Item 1.01 (Entry into a Material Definitive Agreement) — the disclosure "
        "MUST state ALL of:\n"
        "(a) the DATE the agreement was entered into (or amended);\n"
        "(b) the IDENTITY OF THE PARTIES;\n"
        "(c) a brief description of any MATERIAL RELATIONSHIP between the registrant "
        "or its affiliates and any party, OTHER THAN in respect of this agreement. "
        "The contract usually will not state this; include the standard statement "
        "(e.g. 'Other than in respect of the Agreement, there is no material "
        "relationship between the Company and [counterparty].') — it cannot be "
        "verified from the contract, so counsel must confirm it;\n"
        "(d) a brief description of the terms and conditions that are MATERIAL to the "
        "registrant.\n"
        "MATERIALITY TEST for (d): a term is material if there is a substantial "
        "likelihood a reasonable shareholder would consider it important — i.e. its "
        "disclosure would significantly alter the 'total mix' of information.\n\n"
        "MATERIALITY RUBRIC — derived from comparing all of Richtech's own real "
        "Item 1.01 filings against their underlying contracts, i.e. what Richtech's "
        "counsel has actually chosen to disclose in practice:\n"
        "ALWAYS include, when present in the contract:\n"
        "  - the nature of the transaction, in plain language\n"
        "  - the asset(s) involved, WITH quantitative characteristics (square footage, "
        "unit count, quantity) and location\n"
        "  - the purchase price / consideration / financing amount\n"
        "  - the term / duration / maturity\n"
        "  - deposit or earnest money, if any\n"
        "  - conversion or exchange terms and redemption rights, if any\n"
        "USUALLY include (deal-type dependent, use judgment):\n"
        "  - interest rate / discount, and repayment mechanics — ALWAYS for a "
        "promissory note or other debt instrument; usually omitted for services "
        "agreements\n"
        "  - closing / completion timing and conditions — material for asset "
        "purchases; usually omitted for services or financing agreements\n"
        "  - termination rights — material for asset purchases (e.g. a due-diligence "
        "termination right); RICHTECH'S OWN FILINGS RARELY STATE THIS for services "
        "agreements, even when the contract has one, so default to omitting it there\n"
        "RARELY OR NEVER include as individually-described terms — Richtech's own "
        "filings essentially never single these out, REGARDLESS OF DEAL TYPE, even "
        "though the contract always has them — always collapse into a brief catch-all "
        "like 'and other customary provisions' if mentioned at all:\n"
        "  - governing law (0 of 10 real filings that had one stated it)\n"
        "  - assignment / change-of-control provisions\n"
        "  - limitation of liability (as a specific term/cap)\n"
        "  - dispute resolution / arbitration mechanics\n"
        "  - representations, warranties, indemnification, confidentiality — mention "
        "only as part of a brief catch-all, not individually described, UNLESS a "
        "specific provision is genuinely unusual and material (e.g. an uncapped "
        "indemnity, a one-sided term)\n"
        "This rubric reflects a modest sample (17 filings) — when it conflicts with "
        "your own judgment about clear materiality for the specific contract at hand, "
        "prefer INCLUDING an arguably-material term (omission is the greater risk), "
        "but do not use it as license to enumerate everything — the rubric's whole "
        "point is that Richtech's counsel is selective even about facts the general "
        "SEC materiality standard alone would not clearly resolve."
    ),
}


# Richtech includes this exact safe-harbor legend (verbatim, unchanged across every
# real filing that needs it) whenever an Item disclosure contains forward-looking
# language about the Company's own future plans/beliefs -- as opposed to simply
# reciting the agreement's terms, which is a statement of present/historical fact and
# does not require it. Only 3 of Richtech's 17 real Item 1.01 filings have it, always
# tied to the disclosure itself using forward-looking phrasing (e.g. "the Company
# intends to...", "we believe will...", "with the aim of..."), never added by default.
_FORWARD_LOOKING_STATEMENTS = (
    "This Current Report on Form 8-K includes “forward-looking statements” "
    "within the meaning of Section 27A of the Securities Act and Section 21E of the "
    "Securities Exchange Act of 1934, as amended. All statements other than "
    "statements of historical fact included in this Form 8-K are forward-looking "
    "statements. When used in this Form 8-K, words such as “anticipate,” "
    "“believe,” “continue,” “could,” “estimate,” "
    "“expect,” “intend,” “may,” “might,” "
    "“plan,” “possible,” “potential,” “predict,” "
    "“project,” “should,” “would” and similar "
    "expressions, as they relate to us or our management team, identify "
    "forward-looking statements. Such forward-looking statements are based on the "
    "beliefs of the Company's management, as well as assumptions made by, and "
    "information currently available to, the Company's management. Actual results "
    "could differ materially from those contemplated by the forward-looking "
    "statements as a result of certain factors detailed in the Company's filings "
    "with the SEC. All subsequent written or oral forward-looking statements "
    "attributable to the Company or persons acting on its behalf are qualified in "
    "their entirety by this paragraph. Forward-looking statements are subject to "
    "numerous conditions, many of which are beyond the control of the Company, "
    "including those set forth in the “Risk Factors” section of the "
    "Company's Annual Reports on Form 10-K, Quarterly Reports on Form 10-Q and "
    "initial public offering prospectus. The Company undertakes no obligation to "
    "update these statements for revisions or changes after the date of this "
    "release, except as required by law."
)

# Narrower than the boilerplate's own word list (which includes generic modals like
# "may"/"could"/"would" that appear constantly in plain contract-mechanics prose,
# e.g. "the Purchaser may terminate") -- this only matches phrasing that asserts the
# COMPANY's own future plans, intent, or belief, which is what actually triggered the
# legend in Richtech's own real filings.
_FLS_TRIGGER_RE = re.compile(
    r"\b(intends? to|plans? to|expects? to|is expected to|anticipates? that|"
    r"we believe|the company believes?|aims? to|with the aim of|will serve as|"
    r"designed to (?:support|further)|in order to support)\b",
    re.IGNORECASE,
)


def _needs_forward_looking_statements(disclosure: str) -> bool:
    return bool(_FLS_TRIGGER_RE.search(disclosure))


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
    rules = ITEM_RULES.get(item, "(no item-specific requirements on file — apply "
                           "standard 8-K disclosure conventions)")
    return (
        f"=== TARGET: Item {item} — {item_title} ===\n\n"
        f"=== MANDATORY SEC DISCLOSURE REQUIREMENTS (your draft MUST satisfy all) ===\n"
        f"{rules}\n\n"
        f"=== FACTS EXTRACTED FROM THE SOURCE CONTRACT ===\n{_facts_block(review)}\n\n"
        f"=== PRIOR ITEM {item} FILINGS (structure/style reference ONLY — do not "
        f"reuse their facts) ===\n{precedents_block}"
    )


def _compliance_flags(item: str, disclosure: str) -> list[dict]:
    """Lightweight post-check that the drafted disclosure visibly covers the
    mandatory Item requirements, so a reviewer sees a compliance summary rather
    than having to re-derive it. Presence checks only — not a legal judgment."""
    d = disclosure.lower()
    checks: list[tuple[str, bool]] = []
    if item == "1.01":
        has_date = bool(re.search(r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\b", disclosure))
        checks = [
            ("(a) date of agreement", has_date),
            ("(b) parties identified", " between " in d or " with " in d),
            ("(c) material-relationship statement", "material relationship" in d),
            ("(d) material terms described", len(disclosure.split()) > 40),
            ("exhibit incorporation-by-reference", "qualified in its entirety" in d),
        ]
    else:
        checks = [("exhibit incorporation-by-reference", "qualified in its entirety" in d)]
    checks.append(("no unresolved redaction markers", not _HAS_REDACTION_LEAK.search(disclosure)))
    return [{"requirement": name, "satisfied": ok} for name, ok in checks]


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
    disc = result.get("disclosure", "")
    if item == "1.01":
        disc = _ensure_material_relationship(disc)
    result["disclosure"] = _ensure_exhibit_qualifier(disc)
    if _needs_forward_looking_statements(result["disclosure"]):
        result["_forward_looking_statements"] = _FORWARD_LOOKING_STATEMENTS
    result["_compliance"] = _compliance_flags(item, result["disclosure"])
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


_BUSINESS_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {"sentence": {"type": "string"}},
    "required": ["sentence"],
}

_BUSINESS_CONTEXT_SYSTEM = (
    "You are a securities lawyer editing an SEC Form 8-K Item disclosure. Legal or "
    "management has supplied a business-context note explaining the strategic "
    "purpose of this transaction -- something true but not stated anywhere in the "
    "underlying contract (e.g. why an asset matters to the Company's plans). Write "
    "ONE additional sentence, in the same neutral, factual tone as the rest of the "
    "disclosure, that states this business context. Do not add anything beyond "
    "what the note says, do not invent detail, and do not use promotional or "
    "puffery language."
)


def add_business_context(draft: dict, note: str) -> dict:
    """Add a sentence describing the transaction's business/strategic purpose, from
    a note supplied by a human reviewer (legal or management) -- NOT extracted from
    the contract, because this kind of forward-looking narrative (e.g. Richtech's
    real "the Company intends to utilize the Property as a strategic ... facility")
    is routinely present in real filings but never appears in the underlying
    contract, so no document-grounded extraction can produce it. Clearly attributes
    the added sentence to the reviewer's own input (not a contract citation) and
    correctly triggers the Forward-Looking Statements legend, since this is exactly
    the kind of language that requires it.

    Returns a NEW draft dict; does not mutate `draft`."""
    note = (note or "").strip()
    if not note:
        return draft
    item = draft.get("item", "")
    user = (
        f"=== EXISTING ITEM {item} DISCLOSURE ===\n{draft.get('disclosure', '')}\n\n"
        f"=== BUSINESS CONTEXT NOTE FROM LEGAL/MANAGEMENT (not from the contract) ===\n"
        f"{note}\n\nWrite the one sentence to add."
    )
    result = llm.chat_json(_BUSINESS_CONTEXT_SYSTEM, user, _BUSINESS_CONTEXT_SCHEMA,
                            max_tokens=512)
    sentence = result.get("sentence", "").strip()
    if not sentence:
        return draft

    new_draft = dict(draft)
    paras = (new_draft.get("disclosure") or "").split("\n\n")
    if paras:
        paras[0] = paras[0].rstrip() + " " + sentence
    new_draft["disclosure"] = "\n\n".join(paras)
    new_draft["facts_used"] = list(draft.get("facts_used") or []) + [{
        "fact": sentence,
        "source_quote": note,
        "source": "business_context",
        "verified": None,  # not applicable -- not a contract citation
    }]
    new_draft["_business_context_note"] = note
    new_draft["_forward_looking_statements"] = _FORWARD_LOOKING_STATEMENTS
    new_draft["_compliance"] = _compliance_flags(item, new_draft["disclosure"])
    return new_draft
