# Adapter v2 — quality review & guidance for the next training round

**Date:** 2026-07-10
**Reviewed:** `training/adapter-8k-v2/` (bf16 LoRA, r32/α64) against
`training/eval_samples_lf.txt` (adapter OFF vs ON vs the REAL filed 8-K text).
**Audience:** whoever runs training on the RTX PRO 6000 — to decide what to change
before / whether to bake anything else into the adapter.

---

## TL;DR

The adapter is a **clear win on style/structure** but **fabricates specific figures on
number-dense disclosures**. That fabrication is the one thing an 8-K cannot tolerate.
Conclusion: **the adapter should be a STYLE layer only; facts must stay in the
RAG/extraction + verbatim-quote-verification layer, not in the weights.** More training
data will improve style/length/robustness but will **not** make the numbers safe to
trust — that is structural, not a data-volume problem.

---

## What the adapter does well (keep it)

- **Reg-FD / furnishing boilerplate** (Item 7.01) is textbook-correct.
- **Tight framing + "qualified in its entirety by reference to Exhibit …" qualifier.**
- **No `<think>` leakage and no `[Name]/[Date]/$[Amount]` template placeholders** — the
  base model emitted both; the adapter fixed them. This is a real, valuable win.
- Output length tightened toward real-filing length (A/B: avg 2430 → 1098 chars).

## Where it fails (the red line)

| Sample | Verdict | Detail |
|---|---|---|
| AAPL 7.01 (settlement) | OK | Furnishing legend correct; date/exhibit traceable to source |
| AAPL 7.01 (iPhone supply) | Excellent | Matches real; press-release title from source header |
| AAPL 5.02 (cash incentive plan) | Verbose / truncated | Facts source-derived but ran long and **truncated at max_tokens** mid-sentence |
| **AAPL 5.02 (2022 stock plan)** | **Fabricated** | Share count wrong (**500M** vs real **510M** + formula cap **1,274,374,682**); invented an evergreen auto-increase clause; degenerated into a repetitive "whereas the 2014 Plan…" hallucination loop |
| **KSCP 1.01 — acquisition SPA (CORE ITEM)** | **Fabricated** | Re-invented the whole consideration schedule: deferred payment as "**$1M ×4 in 2027**" (real = **$500k/qtr ×8 over 2027–2028**); earn-out "$1M in installments" (real = up to **$2M** tied to 2026 revenue/margin); cash/equity revenue-share figures all wrong; **dropped the $1.1M Frost Bank debt assumption** |
| KSCP 2.01 (completion) | Safe/minimal | "See Item 1.01, incorporated by reference" — legit real-world style, no fabrication, but drops all detail |

**Key tell:** on the KSCP 1.01 case the **base model read the deferred-payment schedule
correctly** while the adapter distorted it. The correct numbers were in the source — the
style-LoRA regularized them into a fluent-but-wrong pattern. Fabrication here is a
generation-channel fidelity problem, not a missing-knowledge problem.

**Mechanical check is not enough:** running our `_compliance_flags` presence-check on the
fabricated KSCP 1.01 text passes **5/6** — it cannot detect format-correct-but-wrong
numbers. Do not rely on it to catch fabrication.

---

## Two questions the RTX side asked

### 1. "Will more training data fix this?"

- **Yes for:** style, tone, materiality/which-terms-to-include, length calibration, and
  the degenerate-repeat loop (sample #4). These are data/scale-fixable.
- **No for:** numeric/date/share-count fidelity. Number-recall only moved 0.577 → 0.675
  (still ~⅓ wrong). Carrying facts through generative weights is lossy by construction;
  no data volume gets you to the ~100% fidelity an 8-K needs.

### 2. "Can RAG be trained inside the adapter?"

- **You cannot bake facts/retrieval into the weights** — that reproduces exactly this
  fabrication problem (facts frozen, un-citable, cross-document leakage).
- **You can and should run adapter + RAG together at inference:** adapter supplies
  style, RAG supplies the source contract + precedents **in the context window**, and the
  model grounds numbers on that in-context text. This is the "RAG as a booster later"
  idea — it's an inference-time composition, not a weight bake-in.
- **Optional training upgrade — RAFT (retrieval-augmented fine-tuning):** train with the
  retrieved context already in the prompt so the adapter learns to **copy from context**
  rather than from memory. This genuinely helps fidelity and is fully RAG-compatible.
  **Even so, a final number-level verification pass is still required** for zero-tolerance
  figures.

---

## Recommendations

**Training side (RTX) — optional, improves quality but not sufficient alone:**
1. Consider **RAFT-style data**: include the source doc (and/or retrieved precedents) in
   the training prompt and reward verbatim copying of figures.
2. Raise `max_tokens` and/or add a "summarize, do not reproduce the full instrument"
   constraint for plan-type Items (5.02) to stop truncation and runaway length.
3. Add more data for style/robustness (step A → v3) — but do **not** expect it to close
   the number-fidelity gap.

**Architecture side (required regardless of training):**
4. Keep `draft.py`'s extraction + `verify_quote` as the **fact source**; use the adapter
   only to polish phrasing/structure.
5. If the adapter drafts directly, add a **number-level reconciliation pass**: match every
   amount / share count / date in the draft against the quote-verified extracted fact set
   and flag any mismatch before a human sees it.

**Do not ship the adapter as a standalone fact source.**
