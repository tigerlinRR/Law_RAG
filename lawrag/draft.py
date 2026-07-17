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
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import guardrail, llm, retrieve
from .config import CONFIG
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
        # general 8-K materiality practice (see ITEM_RULES) discloses when present.
        "Parties", "Effective Date / Signing Date", "Nature of Transaction",
        "Asset(s) Involved (description, size, location, quantity)",
        # For securities sales: capture the per-unit price and count so the drafter
        # states the contract's OWN figures instead of inventing/deriving them.
        "Securities Type / Class (and par value)",
        "Number of Shares or Units Issued",
        "Price per Share / Unit",
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
    "agent", "agents", "designated agent", "sales agent", "placement agent",
    "underwriter", "underwriters", "manager", "managers", "trustee", "escrow agent",
    "purchasers", "sellers", "holders", "investors", "lead agent",
}


# Instrument phrases, most-specific first — used to name the closing qualifier when the
# disclosure defines no non-party term (e.g. it set (the "Purchaser") but not (the "Agreement")).
_INSTRUMENT_WORDS = [
    "Purchase and Sale Agreement", "Securities Purchase Agreement", "Merger Agreement",
    "Credit Agreement", "Asset Purchase Agreement", "Promissory Note", "Agreement", "Note",
    "Indenture", "Warrant", "Lease", "Plan", "Amendment", "Guaranty", "Deed",
]


def _instrument_noun(disclosure: str) -> str:
    """The noun for the closing qualifier / (c) statement — the INSTRUMENT, never a party
    role. Prefer a known instrument phrase actually present in the text (so a party defined
    term like 'Agents'/'Rodman' can never be mistaken for the instrument, regardless of
    whether every role word is in _PARTY_TERMS); else a non-party defined term (for an
    unusual instrument, e.g. (the "Facility")); else 'Agreement'."""
    for w in _INSTRUMENT_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", disclosure):
            return w
    terms = re.findall(r'\(the [“”"]([A-Z][A-Za-z ]+?)[“”"]\)', disclosure)
    noun = next((t for t in terms if t.strip().lower() not in _PARTY_TERMS), None)
    return noun or "Agreement"


def _ensure_exhibit_qualifier(disclosure: str) -> str:
    """Every real 8-K Item disclosure closes with the standard 'qualified in its entirety by
    reference to the full text ... Exhibit 10.1' sentence, referring to the INSTRUMENT. The
    model usually writes it, but (a) sometimes drops it, and (b) sometimes anchors it to a
    PARTY role when the agreement has no defined term (e.g. 'description of the Purchaser') —
    fix both. It's fixed boilerplate, not a fact, so needs no source citation."""
    noun = _instrument_noun(disclosure)
    if "qualified in its entirety" in disclosure.lower():
        # Repair a qualifier the model anchored to a party role instead of the instrument.
        m = re.search(r"description of the ([A-Za-z ]+?) does not purport", disclosure)
        if m and m.group(1).strip().lower() in _PARTY_TERMS:
            wrong = m.group(1)
            disclosure = disclosure.replace(
                f"description of the {wrong} does not purport",
                f"description of the {noun} does not purport")
            disclosure = re.sub(r"full text of such " + re.escape(wrong) + r"\b",
                                f"full text of such {noun}", disclosure)
        return disclosure
    qualifier = (
        f"The foregoing description of the {noun} does not purport to be complete and "
        f"is qualified in its entirety by reference to the full text of such {noun}, a "
        f"copy of which is filed as Exhibit 10.1 to this Current Report on Form 8-K and "
        f"is incorporated herein by reference."
    )
    return disclosure.rstrip() + "\n\n" + qualifier


_ROLE_LABEL_RE = re.compile(
    r"^\s*(?:seller|purchaser|buyer|company|borrower|lender|investor|holder|issuer|"
    r"guarantor|lessor|lessee|vendor|supplier|client|counterparty|party|registrant)\s*[:\-–]\s*",
    re.I)


def _clean_party(p: str) -> str:
    """Strip a leading role label (e.g. 'Seller: ', 'Purchaser - ') and any trailing defined-
    term parenthetical (e.g. ' (the "Rodman")') from a party string."""
    p = _ROLE_LABEL_RE.sub("", (p or "").strip())
    p = re.sub(r'\s*\((?:the\s+)?[“”"][^)]*[“”"]\)\s*$', '', p)  # drop trailing (the "X")
    return p.strip()


def _pick_counterparty(parties: list[str], company_name: str | None) -> str | None:
    """Choose the OTHER party for the Item 1.01(c) statement — the one that is NOT the
    registrant. Blindly taking parties[1] can name the Company itself as its own
    counterparty (the observed bug); match against the registrant name and skip it."""
    cleaned = [c for c in (_clean_party(p) for p in parties) if c]
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    cn = norm(company_name)
    for c in cleaned:
        if cn and (cn in norm(c) or norm(c) in cn):  # this party IS the registrant
            continue
        return c
    return cleaned[1] if len(cleaned) > 1 else (cleaned[0] if cleaned else None)


def _ensure_material_relationship(disclosure: str, counterparty: str | None = None) -> str:
    """Item 1.01(c) requires a statement of any material relationship between the
    registrant and a party OTHER THAN the agreement. The model usually writes it
    but drops it when the precedents don't model it, so guarantee it in standard
    form. It cannot be verified from the contract, so counsel must confirm it —
    but omitting the required (c) element entirely is worse than including the
    standard negative statement for review."""
    if "material relationship" in disclosure.lower():
        return disclosure
    terms = re.findall(r'\(the [“”"]([A-Z][A-Za-z ]+?)[“”"]\)', disclosure)
    instrument = _instrument_noun(disclosure)  # the INSTRUMENT, never a party role
    if counterparty and counterparty.strip():
        cp = _clean_party(counterparty)  # the extracted other party — reliable, not a guess
    else:
        guess = next(
            (t for t in terms if t.strip().lower() in _PARTY_TERMS
             and t.strip().lower() not in {"company", "registrant", "party", "parties",
                                           "sec", "commission"}),
            None)
        cp = f"the {guess}" if guess else "the counterparty"
    cp = cp.rstrip(".").strip() or "the counterparty"  # avoid "Inc.." — sentence adds its own period
    stmt = (f"Other than in respect of the {instrument}, there is no material "
            f"relationship between the Company and {cp}.")
    # Insert before the closing "qualified in its entirety" sentence if present.
    low = disclosure.lower()
    idx = low.find("the foregoing description")
    if idx != -1:
        return disclosure[:idx].rstrip() + "\n\n" + stmt + "\n\n" + disclosure[idx:]
    return disclosure.rstrip() + "\n\n" + stmt

# Field bounds keep the (verbose) 8-K style model from overrunning max_tokens and
# truncating the JSON mid-string. A real Item disclosure is 1-3 tight paragraphs.
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "maxLength": 12},
        "item_title": {"type": "string", "maxLength": 120},
        "disclosure": {"type": "string", "maxLength": 5000},
        "facts_used": {
            "type": "array", "maxItems": 25,
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "maxLength": 400},
                    "source_quote": {"type": "string", "maxLength": 500},
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
    "1. Include the material terms a reasonable investor needs: the nature of the "
    "transaction, the parties, the date, and the key commercial/economic terms material "
    "for this Item type. The extracted facts may include clauses capturing terms SPECIFIC "
    "to THIS agreement (e.g. an exclusivity / sole-agent arrangement and its duration, an "
    "unusual fee or commission, a liability cap, a standstill, a right of first refusal, "
    "an earn-out, a lock-up) — INCLUDE any such term that is material and non-standard; do "
    "NOT drop a clearly material, unusual term just because it is not a routine field. "
    "Leave out only genuine boilerplate.\n"
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
    "fact must come from the source contract facts. Use figures EXACTLY as they appear "
    "in the extracted facts. Do NOT compute, derive, infer, or estimate any number — "
    "e.g. never divide an aggregate price by a per-share price to get a share count, "
    "never total up parts, never guess a par value. If a figure the disclosure would "
    "normally state (share count, price per share, par value, etc.) is not present in "
    "the extracted facts, OMIT it or write '[NOT STATED IN CONTRACT]' — never calculate "
    "or invent it. A missing figure that is flagged is safe; a computed or guessed one "
    "is a compliance error.\n"
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
    "term only by a short generic role-based description, as issuers commonly do "
    "for redacted counterparties — e.g. 'with one of the largest retailers in the world (the "
    "\"Client\")' rather than naming a redacted counterparty. If no natural generic "
    "description is available from the other facts, use a bare defined term like "
    "'a third party (the \"Client\")' with no further description."
)

# Mandatory SEC disclosure requirements per Item (Form 8-K rules + the materiality
# standard from TSC Industries / Basic v. Levinson) PLUS a COMPANY-NEUTRAL, data-derived
# materiality rubric: market-norm disclosure rates measured across ~90 public companies'
# real filings by training/build_general_rubric.py (245 real Item 1.01 disclosures; keyword
# scan of the corpus, deal-type aware). Vendor-neutral by design, so the tool is not biased
# to any single issuer's habits. A specific customer's own style belongs in a facts-stripped
# few-shot layer (roadmap #4), NOT in these materiality rules.
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
        "MATERIALITY GUIDANCE — market norms measured across 245 real Item 1.01 "
        "disclosures from ~90 public companies (company-NEUTRAL; directional bands). "
        "Always state the nature of the transaction, the parties, and the date.\n"
        "Include the following material commercial terms WHENEVER THE CONTRACT HAS THEM "
        "(how often each appears in filings reflects deal type — include it if present):\n"
        "  - price / consideration / financing amount — disclosed in 89% of filings; "
        "state it whenever present\n"
        "  - the asset(s) or securities involved, WITH quantitative characteristics "
        "(square footage, unit/share count, quantity) and location — ~55% overall, 83% "
        "for equity deals\n"
        "  - term / duration / maturity — 60% overall, 77% for debt instruments\n"
        "  - for a DEBT instrument: interest rate / discount and repayment mechanics — "
        "58% for notes/debt (treat as expected for any note)\n"
        "  - conversion / exchange / redemption terms — 57% for debt, 37% overall\n"
        "  - closing / completion timing and conditions — 46% overall, 68% for equity, "
        "high for real-estate purchases\n"
        "  - deposit / earnest money — a real-estate term (low overall only because few "
        "deals are real estate); ALWAYS include it when the contract has one\n"
        "  - a termination right that is material to the deal (e.g. a due-diligence "
        "walk-away and how a deposit is handled on termination) — ~29%\n"
        "Fold these into a brief 'and other customary provisions' catch-all — real "
        "filings almost never describe them individually, REGARDLESS of deal type, so do "
        "NOT single them out unless genuinely unusual AND material (e.g. an uncapped "
        "indemnity, a one-sided term):\n"
        "  - governing law — stated in 0 of 245 filings\n"
        "  - dispute resolution / arbitration / venue — 0.8%\n"
        "  - confidentiality — 6%; assignment / change-of-control — 10%\n"
        "  - representations, warranties, indemnification, limitation of liability — "
        "mention only inside the catch-all (a bare 'customary representations, warranties "
        "and indemnification provisions' is common), never clause-by-clause.\n"
        "When in doubt, prefer INCLUDING an arguably-material term (omission is the "
        "greater risk) — but keep it a brief description of the MATERIAL terms, not a "
        "summary of the contract."
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


def _parse_amount(text: str) -> Decimal | None:
    """Pull the first numeric amount from an extracted value ('$4.55', '$38,675,000.00',
    '$38.675 million') as a Decimal."""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(million|billion)?", text or "", re.I)
    if not m:
        return None
    try:
        val = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    mag = (m.group(2) or "").lower()
    return val * Decimal(1_000_000) if mag == "million" else \
        val * Decimal(1_000_000_000) if mag == "billion" else val


def _find_clause(review: dict, name_contains: str) -> dict | None:
    return next((c for c in review.get("clauses", [])
                 if name_contains.lower() in c.get("name", "").lower()), None)


def _derive_share_count(review: dict) -> None:
    """Securities sales often state a per-share price and an aggregate but NOT a total
    share count (it lives only in per-purchaser schedules), so the model invents one.
    Compute it deterministically (aggregate ÷ price) and add it as a DERIVED fact so the
    drafter states the correct figure. The guardrail recognizes it as derived
    (arithmetic of two verbatim source figures) -> non-blocking, arithmetic shown."""
    shares = _find_clause(review, "Number of Shares")
    if shares and shares.get("value", "").strip().lower() not in ("", "not found"):
        return  # contract states it explicitly
    price_c, agg_c = _find_clause(review, "Price per Share"), _find_clause(review, "Purchase Price")
    if not (price_c and agg_c):
        return
    price, agg = _parse_amount(price_c.get("value", "")), _parse_amount(agg_c.get("value", ""))
    if not price or not agg or price <= 0:
        return
    count = agg / price
    nearest = round(count)
    if nearest <= 0 or abs(count - nearest) > count * Decimal("0.005"):
        return  # not a clean whole-share division -> don't guess
    desc = f"= {agg_c['value'].strip()} ÷ {price_c['value'].strip()}"
    review.setdefault("clauses", []).append({
        "name": "Number of Shares or Units Issued (derived)",
        "value": (f"{nearest:,} shares — derived as {agg_c['value'].strip()} ÷ "
                  f"{price_c['value'].strip()}; both stated in the contract. "
                  "State this share count."),
        "quote": "",
    })
    # Anchored derivation for the guardrail: ONLY this specific value counts as
    # "derived" (grounded); the model's inventions stay fabricated -> RED.
    review.setdefault("_derived", []).append((Decimal(nearest), desc))


_FIGURE_PLACEHOLDER = "[NOT IN SOURCE — CONFIRM]"


def _lock_figures(disclosure: str, source_text: str, derived: list | None) -> tuple[str, list[str]]:
    """HYBRID mode: keep the model's prose, but neutralize every FIGURE it produced that
    is not grounded in the source (or a valid derivation) — replace it with a visible
    placeholder so no imagined number survives as a plausible value. Returns the locked
    text + the list of blanked figures (so the UI can tell the reviewer to fill them).
    (Only figures are locked; a qualitative mis-statement — e.g. "registered offering" —
    is not a number and still needs human review.)"""
    r = guardrail.reconcile(disclosure, source_text, derived=derived)
    fabricated = sorted({i["raw"] for i in r["items"] if i["status"] == "fabricated"},
                        key=len, reverse=True)  # longest first so substrings don't clash
    locked = disclosure
    for raw in fabricated:
        locked = locked.replace(raw, _FIGURE_PLACEHOLDER)
    return locked, fabricated


def _num_only(s: str) -> str:
    m = re.search(r"[\d,]+(?:\.\d+)?", s or "")
    return m.group(0) if m else (s or "").strip()


def _assemble_disclosure(item: str, review: dict, item_title: str) -> tuple[str, list[dict]]:
    """FACT-LOCKED drafting: build the disclosure deterministically from the VERIFIED
    extracted clauses — the model never writes a figure, so it cannot imagine one. Prose
    is templated (a lawyer polishes wording, not facts). Every stated fact is cited to its
    verbatim source quote. Missing facts are simply omitted, never invented."""
    def val(name: str) -> str:
        c = _find_clause(review, name)
        v = (c or {}).get("value", "").strip()
        return "" if v.lower() in ("", "not found") else v

    facts: list[dict] = []
    def cite(name: str, fact_text: str = "") -> None:
        c = _find_clause(review, name)
        q = (c or {}).get("quote", "")
        if q:
            facts.append({"fact": fact_text or (c or {}).get("value", ""), "source_quote": q})

    parties = [p for p in review.get("parties", []) if p.strip()]
    company = parties[0] if parties else "the Company"
    counterparty = parties[1] if len(parties) > 1 else ""
    date = val("Effective Date / Signing Date")
    raw_type = (review.get("doc_type") or "").strip()
    agreement = raw_type if any(k in raw_type.lower() for k in
                                ("agreement", "note", "plan", "lease", "indenture", "purchase")) \
        else "definitive agreement"
    term = "Agreement"  # defined term used in the qualifier + material-relationship sentence

    s = (f"On {date}, " if date else "") + \
        f"{company} (the “Company”) entered into a {agreement} (the “{term}”)"
    if counterparty:
        s += f" with {counterparty}"

    # securities sale: number of shares (extracted or derived) / class / price / aggregate
    shares = _num_only(val("Number of Shares or Units Issued"))
    if not shares and review.get("_derived"):
        shares = f"{review['_derived'][0][0]:,}"
    cls, pps, agg = (val("Securities Type / Class (and par value)"),
                     val("Price per Share / Unit"), val("Purchase Price / Consideration"))
    asset = val("Asset(s) Involved (description, size, location, quantity)")
    principal, rate, maturity = (val("Financing Amount / Principal"),
                                 val("Interest Rate / Discount"), val("Maturity / Term / Duration"))

    if shares or pps:
        s += ", pursuant to which the Company agreed to issue and sell"
        s += f" {shares} shares" if shares else " shares"
        if cls:
            s += f" of {cls}"
        if pps:
            s += f" at a purchase price of {pps} per share"
        if agg:
            s += f", for aggregate gross proceeds of {agg}"
    elif asset:
        s += f" to acquire {asset}"
        if agg:
            s += f" for a purchase price of {agg}"
    elif agg:
        s += f" providing for aggregate consideration of {agg}"
    s += "."
    for n in ("Effective Date / Signing Date", "Securities Type / Class (and par value)",
              "Price per Share / Unit", "Purchase Price / Consideration",
              "Asset(s) Involved (description, size, location, quantity)"):
        cite(n)
    if shares:
        c = _find_clause(review, "Number of Shares or Units Issued")
        if c and c.get("quote"):
            cite("Number of Shares or Units Issued")
    sentences = [s]

    if principal or rate or maturity:  # note / financing obligation
        bits = []
        if principal:
            bits.append(f"a principal amount of {principal}"); cite("Financing Amount / Principal")
        if rate:
            bits.append(f"interest at {rate}"); cite("Interest Rate / Discount")
        if maturity:
            bits.append(f"a maturity of {maturity}"); cite("Maturity / Term / Duration")
        sentences.append(f"The {agreement} provides for " + ", ".join(bits) + ".")

    return "\n\n".join(sentences), facts


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


# Roadmap #6 — narrative-claim verification. The numeric guardrail locks fabricated FIGURES,
# but a model can still invent a non-numeric claim (or a spelled-out number like "ten business
# days") — e.g. a termination-notice period the contract doesn't contain. This LLM audit pass
# flags substantive claims the source doesn't support. REVIEW-ONLY: it never blocks and never
# alters the draft — a purely additive safety net, so it is always safe to run.
_NARRATIVE_SKIP = re.compile(
    r"qualified in its entirety|does not purport to be complete|material relationship between|"
    r"forward-looking statements|incorporated herein by reference|Exhibit 10\.1", re.I)

_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {"unsupported": {
        "type": "array", "maxItems": 20,
        "items": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "issue": {"type": "string", "maxLength": 300},
        }, "required": ["index", "issue"]}}},
    "required": ["unsupported"]}

_NARRATIVE_SYSTEM = (
    "You audit a draft SEC Form 8-K disclosure for FABRICATION. You are given numbered claims "
    "from the draft, plus the GROUNDED FACTS (terms extracted verbatim from the source "
    "contract) — those facts are the ONLY supported source of truth. For each claim, decide "
    "whether the grounded facts support it. Flag a claim as unsupported ONLY if it asserts a "
    "specific factual term — a number, date, party, right, obligation, amount, duration, fee, "
    "or condition — that the grounded facts do NOT contain, i.e. it appears invented. DO NOT "
    "flag: general framing (e.g. 'the Company entered into an agreement'), standard 8-K "
    "boilerplate, or a reasonable paraphrase of a fact that IS present. Be conservative — if a "
    "claim is plausibly supported by the facts, treat it as supported. For each unsupported "
    "claim return its NUMBER in 'index' and the specific unsupported part in 'issue'.")


def _narrative_flags(disclosure: str, evidence: str) -> list[dict]:
    """Flag substantive claims in `disclosure` not supported by `evidence` (the extracted,
    quote-verified grounded facts + any reviewer supplements/business context). Checking
    against the compact grounded facts — NOT the raw contract — avoids long-document
    windowing false-positives and matches what the draft was actually built from. Review-only
    (never blocks). Boilerplate / (c) statement / FLS legend / exhibit qualifier are skipped."""
    if not evidence.strip() or not disclosure.strip():
        return []
    sents = [s.strip() for s in re.split(r"(?<=[.;])\s+", disclosure) if len(s.strip()) > 25]
    claims = [s for s in sents if not _NARRATIVE_SKIP.search(s)]
    if not claims:
        return []
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
    user = (f"=== DRAFT CLAIMS ===\n{numbered}\n\n"
            f"=== GROUNDED FACTS (the only supported source) ===\n{evidence[:CONFIG.llm_max_ctx_chars]}")
    try:
        out = llm.chat_json(_NARRATIVE_SYSTEM, user, _NARRATIVE_SCHEMA, max_tokens=1500)
    except Exception:
        return []
    flags = []
    for f in out.get("unsupported", []):
        idx = f.get("index")
        if isinstance(idx, int) and 1 <= idx <= len(claims):
            flags.append({"claim": claims[idx - 1], "issue": f.get("issue", "")})
    return flags


def draft_8k(
    contract_path: str | Path,
    item: str = "1.01",
    n_precedents: int = 0,
    allowed_clients: list[str] | None = None,
    exclude_document_ids: list[int] | None = None,
    mode: str = "hybrid",
) -> dict:
    """Draft an 8-K Item disclosure for `contract_path`, grounded in facts extracted
    from that contract.

    `n_precedents` defaults to 0: the fine-tuned 8-K adapter already carries the filing
    STYLE in its weights, so in-prompt precedents are redundant — and worse, the model
    copied their FACTS (share counts, file numbers, registered-vs-private-placement)
    into the draft, contradicting the source contract (verified precedent fact-leakage).
    Set n_precedents>0 only for the un-adapted base model. `exclude_document_ids`:
    for held-out evaluation — exclude the real 8-K this contract produced.

    Facts always come from the source contract; the guardrail (lawrag.guardrail) flags
    any figure not grounded verbatim (incl. correct-but-derived ones) for human review."""
    item_title = ITEM_TITLES.get(item, "")
    precedent_citations: list[str] = []

    if mode == "delex":
        # v4: delex the source (regex + spaCy — NO LLM extraction) -> the model emits a
        # placeholder skeleton (cannot write a real value) -> backfill placeholders from
        # the source map. Facts come from the source, structure from v4. Needs ONLY the v4
        # endpoint (config.llm_v4_*); does not touch the extraction model, so no 2-model
        # GPU contention with the main model.
        from . import delex_backfill as _bf
        from .parsers import parse as _parse
        full_text = "\n\n".join(b.text for b in _parse(Path(contract_path)))
        delexed, smap = _bf.delex_source(full_text[:_bf.SOURCE_WINDOW])
        skeleton = llm.chat(
            _bf.SYSTEM,
            f"Draft the Item {item} disclosure.\n\n=== SOURCE DOCUMENT ===\n{delexed}",
            temperature=0.0, max_tokens=2048,
            base_url=CONFIG.llm_v4_base_url, model=CONFIG.llm_v4_model)
        disclosure, missing = _bf.backfill(skeleton, smap)
        result = {"disclosure": disclosure, "facts_used": [], "_backfill_missing": missing}
        review = {"_full_text": full_text, "parties": [], "clauses": [],
                  "doc_type": "", "summary": "", "_derived": []}
    else:
        review = review_contract(contract_path, checklist=_checklist_for(item))
        if item in ("1.01", "3.02"):  # securities sales: supply the derived share count
            _derive_share_count(review)

    if mode == "assemble":
        # A) FACT-LOCKED: assemble from verified facts; the model writes no prose at all.
        disc, facts = _assemble_disclosure(item, review, item_title)
        result = {"disclosure": disc, "facts_used": facts}
    elif mode in ("hybrid", "llm"):  # the model drafts the prose (in its 8-K style)
        precedent_texts: list[str] = []
        if n_precedents > 0:  # opt-in; skipping avoids needing the DB/retrieval stack
            hits = retrieve.search(
                f"8-K Item {item} {item_title}",
                filters=retrieve.Filters(doc_type="8-K"),
                top_k=n_precedents * 4,
                allowed_clients=allowed_clients,
                meta_filters={"filing_items": item},
                exclude_document_ids=exclude_document_ids,
                use_rerank=False,
            )
            by_doc: dict[int, list] = {}
            for h in hits:
                by_doc.setdefault(h.document_id, []).append(h)
            precedent_docs = list(by_doc.values())[:n_precedents]
            precedent_texts = ["\n".join(c.content for c in chs) for chs in precedent_docs]
            precedent_citations = [chs[0].citation() for chs in precedent_docs]
        result = llm.chat_json(
            _SYSTEM, _user_prompt(item, item_title, review, precedent_texts),
            DRAFT_SCHEMA, max_tokens=8192,
        )
    # Item/title are known inputs, not model output — set them deterministically
    # rather than trust free-form generation (which sometimes echoes precedent text).
    result["item"] = item
    result["item_title"] = item_title
    disc = result.get("disclosure", "")
    if item == "1.01":
        from .export import load_registrant  # lazy: avoid a circular import at module load
        _counterparty = _pick_counterparty(review.get("parties", []), load_registrant().get("name"))
        disc = _ensure_material_relationship(disc, _counterparty)
    disc = _ensure_exhibit_qualifier(disc)
    full_text = review.get("_full_text", "")
    if mode == "hybrid":
        # HYBRID (default): keep the model's prose but HARD-LOCK figures — any number it
        # produced that isn't grounded in the source is blanked to a placeholder, so no
        # imagined figure survives. The reviewer then fills the placeholders.
        disc, result["_blanked_figures"] = _lock_figures(disc, full_text, review.get("_derived"))
    result["disclosure"] = disc
    if _needs_forward_looking_statements(result["disclosure"]):
        result["_forward_looking_statements"] = _FORWARD_LOOKING_STATEMENTS
    result["_compliance"] = _compliance_flags(item, result["disclosure"])
    result["_repaired"] = review.get("_repaired")  # count of verify-gated 2nd-pass repairs
    # Fact-fidelity guardrail on the (figure-locked) disclosure.
    result["_guardrail"] = guardrail.reconcile(
        result["disclosure"], full_text, derived=review.get("_derived"))
    # v4/delex: a placeholder v4 emitted that the source map lacked is a fact NOT in the
    # source -> BLOCK (composes with the reconciliation guardrail's RED logic).
    for ph in result.get("_backfill_missing") or []:
        result["_guardrail"]["items"].append(
            {"raw": ph, "normalized": ph, "kind": "placeholder",
             "status": "fabricated", "source_snippet": None})
    if result.get("_backfill_missing"):
        result["_guardrail"]["verdict"] = "blocked"
    # #6 narrative-claim audit (model-authored prose only; assemble is deterministic).
    # Check against the extracted grounded facts (compact, complete) not the raw contract.
    if mode in ("hybrid", "llm"):
        result["_grounded_facts"] = _facts_block(review)
        result["_narrative_flags"] = _narrative_flags(result["disclosure"], result["_grounded_facts"])
    # Keep the source text + anchored derivations so an edited draft can be re-verified
    # in-app (POST /reverify) without re-parsing the contract.
    result["_source_text"] = full_text
    result["_derived_values"] = [[str(v), d] for v, d in review.get("_derived", [])]
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


# --- multi-Item filing assembly ------------------------------------------------
# Items that conventionally incorporate a companion Item by reference when co-filed
# (the SAME transaction disclosed under a second Item — e.g. an SPA is both a material
# agreement (1.01) and an unregistered sale (3.02)). item -> companions in priority.
_CROSS_REF_TO = {
    "3.02": ["1.01", "2.03", "2.01"],
    "2.01": ["1.01"],
    "2.03": ["1.01"],
}


def _cross_ref_companion(item: str, selected: list[str]) -> str | None:
    """If `item` should incorporate another selected Item by reference (same
    transaction), return that companion Item; else None (draft it substantively)."""
    for comp in _CROSS_REF_TO.get(item, []):
        if comp in selected and comp != item:
            return comp
    return None


def _cross_ref_text(item: str, companion: str) -> str:
    return (f"The information set forth under Item {companion} of this Current Report on "
            f"Form 8-K is incorporated by reference into this Item {item}.")


def _filing_order(items: list[str]) -> list[str]:
    """Dedupe + sort selected Items into filing order (ascending Item number)."""
    uniq = list(dict.fromkeys(i for i in items if i in ITEM_TITLES))
    return sorted(uniq, key=lambda s: [int(x) for x in s.split(".")])


def draft_filing(contract_path: str | Path, items: list[str],
                 allowed_clients: list[str] | None = None) -> dict:
    """Draft a multi-Item 8-K from ONE source contract.

    Substantive Items are drafted from the contract; recognized cross-reference Items
    (e.g. 3.02 -> 1.01) get the standard 'incorporated by reference' boilerplate — no
    LLM, no fabrication risk. Returns one result dict whose top-level fields carry the
    PRIMARY substantive Item (so History / guardrail banner / review pack keep working),
    plus `_items`: the ordered list of {item, item_title, disclosure, cross_ref} sections
    that form the filing body. Guardrails from all substantive Items are merged."""
    items = _filing_order(items) or ["1.01"]
    sections: list[dict] = []
    substantive: list[tuple[str, dict]] = []
    for it in items:
        title = ITEM_TITLES.get(it, "")
        comp = _cross_ref_companion(it, items)
        if comp:
            sections.append({"item": it, "item_title": title,
                             "disclosure": _cross_ref_text(it, comp), "cross_ref": True})
        else:
            r = draft_8k(contract_path, item=it, allowed_clients=allowed_clients)
            substantive.append((it, r))
            sections.append({"item": it, "item_title": r.get("item_title", title),
                             "disclosure": r.get("disclosure", ""), "cross_ref": False})
    if not substantive:  # degenerate (only cross-ref Items selected): draft the first
        it = items[0]
        r = draft_8k(contract_path, item=it, allowed_clients=allowed_clients)
        substantive.append((it, r))
        for s in sections:
            if s["item"] == it:
                s["disclosure"], s["cross_ref"] = r.get("disclosure", ""), False

    primary_item, primary = substantive[0]
    result = dict(primary)
    merged, blocked = [], False
    for _, r in substantive:
        g = r.get("_guardrail") or {}
        merged.extend(g.get("items", []))
        blocked = blocked or g.get("verdict") == "blocked"
    result["_guardrail"] = {"verdict": "blocked" if blocked else "clean", "items": merged}
    result["_items"] = sections
    result["item"] = primary_item
    result["item_title"] = ITEM_TITLES.get(primary_item, "")
    return result


_BUSINESS_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {"paragraph": {"type": "string"}, "added_text": {"type": "string"}},
    "required": ["paragraph", "added_text"],
}

_BUSINESS_CONTEXT_SYSTEM = (
    "You are a securities lawyer editing an SEC Form 8-K Item disclosure. Legal or "
    "management has supplied a business-context note explaining the strategic "
    "purpose of this transaction -- something true but not stated anywhere in the "
    "underlying contract (e.g. why an asset matters to the Company's plans).\n\n"
    "You are given the disclosure's opening paragraph. Rewrite it to weave in this "
    "business context, the way a real 8-K does -- typically one to two sentences, "
    "placed at the most natural point (often right after describing the asset/"
    "subject matter of the transaction, before the financial/closing terms), not "
    "tacked onto the end. Do NOT write a separate bolted-on sentence at the end "
    "unless that is genuinely the most natural place for it.\n\n"
    "CRITICAL: every existing fact, name, date, amount, and defined term in the "
    "paragraph below MUST remain, unchanged, in your rewrite -- you are ONLY "
    "inserting the business-context material, never removing, shortening, or "
    "rephrasing the existing facts. Keep the same neutral, factual tone. Do not "
    "add anything beyond what the note says, do not invent detail, and do not use "
    "promotional or puffery language.\n\n"
    "Return 'paragraph' (the full rewritten paragraph) and 'added_text' (just the "
    "new sentence(s) you inserted, verbatim as they appear in 'paragraph')."
)

_NUMERIC_TOKEN_RE = re.compile(r"\d[\d,.]*\d|\d")


def _preserves_facts(original: str, revised: str) -> bool:
    """Cheap, reliable safety check: every number that appeared in the original
    paragraph (prices, dates, quantities -- the things a rewrite must never lose or
    alter) must still appear in the revised one. Doesn't guarantee wording is
    untouched, but reliably catches the failure mode that matters: a dropped or
    silently-changed figure in an SEC filing."""
    orig_nums = set(_NUMERIC_TOKEN_RE.findall(original))
    revised_nums = set(_NUMERIC_TOKEN_RE.findall(revised))
    return orig_nums.issubset(revised_nums)


def add_business_context(draft: dict, note: str) -> dict:
    """Merge business/strategic-purpose context into the disclosure, from a note
    supplied by a human reviewer (legal or management) -- NOT extracted from the
    contract, because this kind of forward-looking narrative (e.g. Richtech's real
    "the Company intends to utilize the Property as a strategic ... facility") is
    routinely present in real filings but never appears in the underlying contract,
    so no document-grounded extraction can produce it. Integrates it naturally (one
    to a few sentences at the appropriate point, not bolted onto the end) while
    verifying every existing fact survives the rewrite untouched; falls back to a
    plain append if that check fails, so a filing can never silently lose a fact.
    Clearly attributes the added text to the reviewer's own input (not a contract
    citation) and correctly triggers the Forward-Looking Statements legend.

    Returns a NEW draft dict; does not mutate `draft`."""
    note = (note or "").strip()
    if not note:
        return draft
    item = draft.get("item", "")
    paras = (draft.get("disclosure") or "").split("\n\n")
    if not paras:
        return draft
    opening = paras[0]
    user = (
        f"=== OPENING PARAGRAPH OF THE ITEM {item} DISCLOSURE ===\n{opening}\n\n"
        f"=== BUSINESS CONTEXT NOTE FROM LEGAL/MANAGEMENT (not from the contract) ===\n"
        f"{note}"
    )
    result = llm.chat_json(_BUSINESS_CONTEXT_SYSTEM, user, _BUSINESS_CONTEXT_SCHEMA,
                            max_tokens=800)
    revised = (result.get("paragraph") or "").strip()
    added_text = (result.get("added_text") or "").strip()
    if not revised or not added_text:
        return draft
    if _preserves_facts(opening, revised):
        paras[0] = revised
    else:
        # Rewrite dropped/changed a figure -- don't risk it silently; append instead.
        paras[0] = opening.rstrip() + " " + added_text

    new_draft = dict(draft)
    new_draft["disclosure"] = "\n\n".join(paras)
    new_draft["facts_used"] = list(draft.get("facts_used") or []) + [{
        "fact": added_text,
        "source_quote": note,
        "source": "business_context",
        "verified": None,  # not applicable -- not a contract citation
    }]
    new_draft["_business_context_note"] = note
    new_draft["_forward_looking_statements"] = _FORWARD_LOOKING_STATEMENTS
    new_draft["_compliance"] = _compliance_flags(item, new_draft["disclosure"])
    return new_draft
