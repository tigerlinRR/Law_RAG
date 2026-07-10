"""Acceptance tests for lawrag.guardrail against training/GUARDRAIL_SPEC.md Sec.6.

Floor cases only (6A must-flag, 6B must-not-flag). The spec is the contract; these
thresholds/cases are copied from it verbatim, not re-defined here. Run:
    ./.venv/bin/python tests/test_guardrail.py
Exits non-zero if any case fails.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lawrag.guardrail import reconcile  # noqa: E402


def _statuses(draft, source):
    r = reconcile(draft, source)
    return r["verdict"], {(i["kind"], i["normalized"]): i["status"] for i in r["items"]}


def has_fabrication(draft, source, normalized_value):
    _, st = _statuses(draft, source)
    return any(k[1] == normalized_value and v == "fabricated" for k, v in st.items())


def has_omission(draft, source, normalized_value):
    _, st = _statuses(draft, source)
    return any(k[1] == normalized_value and v == "omitted" for k, v in st.items())


def flagged_any(draft, source):
    v, _ = _statuses(draft, source)
    return v != "clean"


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---- 6A: MUST FLAG -----------------------------------------------------------
@case
def TP1_kscp_fabricated_installment():
    src = ("Deferred cash payments of $500,000 per quarter, totaling $4,000,000, "
           "payable in eight installments from 2027-03-31 through 2028-12-31.")
    draft = ("$4,000,000 in four equal installments of $1,000,000 on March 31, 2027, "
             "June 30, 2027, September 30, 2027 and December 31, 2027.")
    # $1,000,000 is not grounded in the source -> RED fabrication
    assert has_fabrication(draft, src, "1000000"), "should flag $1,000,000 fabrication"
    # $4,000,000 total IS in source -> must not be flagged
    v, st = _statuses(draft, src)
    assert st.get(("currency", "4000000")) == "matched", "$4,000,000 should match"
    assert v == "blocked"


@case
def TP2_kscp_frost_debt_omission_REDONLY_clean():
    # Spec §4 (amended 2026-07-10): omission is REVIEW-ONLY and never blocks. In the
    # shipped RED-only mode (no rubric mapping), a pure omission does NOT flag.
    src = ("At closing the Buyer discharged $1,100,000 of indebtedness owed to "
           "Frost Bank. Closing cash payment of $5,000,000.")
    draft = "The Company paid $5,000,000 in cash at closing."
    v, _ = _statuses(draft, src)
    assert v == "clean", "RED-only: a pure omission must not block"


@case
def TP2_kscp_frost_debt_omission_OPTIONB():
    # With the rubric MUST-disclose mapping supplied (Option B), the assumed-debt
    # omission surfaces as AMBER -- still never blocking (verdict stays clean).
    src = ("At closing the Buyer discharged $1,100,000 of indebtedness owed to "
           "Frost Bank. Closing cash payment of $5,000,000.")
    draft = "The Company paid $5,000,000 in cash at closing."
    r = reconcile(draft, src, must_disclose={"indebtedness", "debt"})
    assert r["verdict"] == "clean", "omission never blocks, even under Option B"
    assert any(i["normalized"] == "1100000" and i["status"] == "omitted"
               for i in r["items"]), "scoped AMBER should surface the $1.1M debt"


@case
def TP3_aapl_share_count():
    src = ("The 2022 Plan authorizes 510,000,000 shares, with a maximum of "
           "1,274,374,682 shares issuable under the formula.")
    draft = "The 2022 Plan authorizes up to 500 million shares of common stock."
    assert has_fabrication(draft, src, "500000000"), "500M != 510M -> RED fabrication"
    # cap omission is Option-B AMBER (review-only); under Option B it surfaces but the
    # RED fabrication is what blocks.
    r = reconcile(draft, src, must_disclose={"maximum"})
    assert any(i["normalized"] == "1274374682" and i["status"] == "omitted"
               for i in r["items"]), "scoped AMBER should surface the cap"
    assert r["verdict"] == "blocked", "the 500M RED still blocks"


@case
def TP4_aapl_evergreen_percent():
    src = "The 2022 Plan authorizes 510,000,000 shares of common stock."
    draft = ("The plan provides an automatic annual increase of 1% of shares "
             "outstanding.")
    assert has_fabrication(draft, src, "0.01"), "invented 1% -> fabrication"


# ---- 6B: MUST NOT FLAG -------------------------------------------------------
@case
def TN1_currency_word_form():
    assert not flagged_any("consideration of $5.0 million", "a price of $5,000,000")


@case
def TN2_share_suffix():
    assert not flagged_any("1,724,418 shares of Class A Common Stock", "1,724,418")


@case
def TN3_date_format():
    assert not flagged_any("dated April 29, 2024", "on 2024-04-29")


@case
def TN4_par_value():
    assert not flagged_any("par value $0.001 per share", "par value of $0.001")


@case
def TN5_identifiers_not_facts():
    src = "under Item 1.01, Rule 3b-7 and Section 18 of the Exchange Act"
    draft = "pursuant to Item 1.01, Rule 3b-7 and Section 18 of the Exchange Act"
    assert not flagged_any(draft, src), "section ids / rule cites must not be facts"


def main():
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
