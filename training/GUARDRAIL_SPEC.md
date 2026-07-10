# GUARDRAIL_SPEC — 8-K fact reconciliation (acceptance criteria + test cases)

**Author (defines correctness):** RTX/training side. **Implements:** Jetson/app side.
**Status:** contract. This document specifies WHAT the guardrail must do and the
tests it must pass. It deliberately does **not** prescribe implementation, libraries,
or code structure, and **does not modify `draft.py` or any app code** — that is the
implementer's job (kept separate on purpose: the definer does not grade their own work).

---

## 1. Purpose & role

The 8-K style adapter is **style-only and fabricates figures** (verified: see
`ADAPTER_V2_REVIEW.md` + `eval_samples_lf.txt`). The guardrail is the **grounding /
fact-fidelity layer** that runs AFTER drafting and BEFORE a human sees the draft.

- It reconciles every **material datum** in the DRAFT against the **SOURCE document**
  (the contract/press-release that was fed into the prompt) — *not* against a knowledge
  base. **No DB, no embedding, no retrieval.** Pure local text processing; edge-safe.
- It is **orthogonal to the vector stack**: cutting pgvector/embed/rerank does NOT cut
  this. "No RAG" ≠ "no fact-check."
- It does **not** replace lawyer sign-off; it flags before a human, never auto-files.

## 2. Scope — what is a "material datum"

Reconcile these kinds:
- **currency amounts** ($ values), **share/unit counts**, **percentages**, **dates**,
  and **named parties** (entity names).

Explicitly OUT of scope (must NOT be treated as facts to reconcile, must NOT flag):
- section identifiers (`Item 7.01`, `Item 1.01`), rule/statute citations (`Rule 3b-7`,
  `Section 18`, `Section 409A`), form names (`Form 8-K`, `S-3`), and generic boilerplate.

## 3. Normalization (canonicalize BEFORE comparing — this is the crux)

Filings reformat figures; a naive substring match false-flags correct values. Normalize
both draft and source to canonical values, then compare:

| Kind | Canonical form | Equivalences that MUST match |
|---|---|---|
| currency | integer dollars | `$5,000,000` = `$5.0 million` = `$5.0\nmillion` = `5,000,000` (in $ context) |
| count | integer | `1,724,418` = `1,724,418 shares`; `510,000,000` = `510 million` |
| percent | decimal | `2.5%` = `2.5 percent` = `0.025` |
| date | ISO `YYYY-MM-DD` | `April 29, 2024` = `April 29th, 2024` = `2024-04-29` |

Rounding tolerance: a figure written at precision P (e.g. `$5.0 million`, one decimal)
matches any source value that rounds to it at P (e.g. source `$5,012,000` → `$5.0M` OK).
Exact figures (`$5,000,000`, `1,724,418`, share counts, explicit dates) match exactly.
Handle line-break-split numbers (`$5.0\nmillion`) and par values (`$0.001`).

## 4. Two-way check & severity

| Check | Definition | Severity |
|---|---|---|
| **FABRICATION** | a material datum in the DRAFT with no normalized match in the SOURCE | **RED — block** |
| **OMISSION** | a material datum in the SOURCE, of a kind the disclosure carries, absent from the DRAFT | **AMBER — review** |

RED blocks the draft from reaching a human as "ready"; AMBER surfaces for review.
(Presence-only compliance checks like `_compliance_flags` pass fabricated text 5/6 — they
CANNOT catch format-correct-but-wrong numbers. This value-level check is what does.)

## 5. Output contract

For each reconciled datum return at least: `{raw, normalized, kind, status ∈
{matched, fabricated, omitted}, source_snippet | null}`. Plus an overall verdict:
`clean | needs_review | blocked`. Exact schema/shape is the implementer's choice.

## 6. Acceptance test cases (the contract)

Ground truth = the SOURCE document (`training/dataset/val.jsonl` `input` field; drafts in
`training/eval_samples_lf.txt`; real contracts in `data/RR contracts/`).

### 6A. MUST FLAG (true positives — guardrail fails if it misses any)

| ID | Source says | Draft (adapter) says | Expected |
|---|---|---|---|
| TP1 | KSCP 1.01: deferred = **$500,000 per quarter**, 8 installments, 2027-03-31 → 2028-12-31 | `$4,000,000 in four equal installments of $1,000,000 … 2027` | RED fabrication ($1,000,000 & "four" & 2027-only not grounded) |
| TP2 | KSCP 1.01: **Frost Debt discharged by Buyer** (~$1.1M debt assumption) | (omitted entirely) | AMBER omission (material) |
| TP3 | AAPL 5.02: 2022 plan = **510,000,000 shares** + cap **1,274,374,682** | `500` million shares; cap missing | RED fabrication (500M≠510M) + AMBER omission (cap) |
| TP4 | AAPL 5.02: no "1% evergreen auto-increase" clause in source | invents a `1%` annual auto-increase | RED fabrication (1% not in source) |

### 6B. MUST NOT FLAG (true negatives — guardrail fails if it flags any)

| ID | Source | Draft | Expected |
|---|---|---|---|
| TN1 | `$5,000,000` | `$5.0 million` | matched (normalized equal) |
| TN2 | `1,724,418` (shares) | `1,724,418 shares` | matched |
| TN3 | `2024-04-29` | `April 29, 2024` | matched |
| TN4 | `$0.001` par value | `$0.001 per share` | matched |
| TN5 | source contains `Item 1.01`, `Rule 3b-7`, `Section 18` | draft repeats them | not treated as facts → no flag |

A correct implementation catches all of 6A and stays silent on all of 6B. Add more cases
freely, but these are the floor.

## 7. Non-goals

- Does not dictate parsing library, module layout, or how it hooks into `verify_quote`.
- Does not touch retrieval, the served model, or training.
- Does not remove the mandatory lawyer sign-off.
