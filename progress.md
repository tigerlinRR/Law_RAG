# Law_RAG — Progress / Restart Context

Living status doc so a fresh chat can resume fast. Pairs with:
- **Auto-memory** `~/.claude/projects/-home-jetson-Desktop-Law-RAG/memory/` (read
  `MEMORY.md` index; the dense history is in `law-rag-project-plan.md`).
- **`CLAUDE.md`** (points here) and the repo `README.md` (product/architecture docs).

_Last updated: 2026-07-17._

## ⭐ CURRENT STATE (2026-07-17) — READ THIS FIRST
**Architecture is SETTLED (v1-spine): the model UNDERSTANDS/extracts, CODE generates,
the guardrail backstops, humans fill gaps. Facts never come from model weights.**
- **Served model:** the PLAIN BASE `qwen3.6` (container `lawrag-llm`, :8012). The v2 style
  adapter and the delex v4/v5 idea are **RETIRED** (fine-tuning fabricates; delex only worked
  for transaction Items — both dead ends, kept only for history). `.env LLM_MODEL=qwen3.6`.
- **Default drafting = `draft_8k(mode="hybrid")`**: base model drafts in 8-K style →
  `_lock_figures` blanks ungrounded numbers → numeric guardrail (RED blocks fabrication) →
  `_narrative_flags` (#6, review-only, flags invented non-numeric claims). `assemble` = optional
  fully-deterministic mode.
- **Shipped & working (all this on the base model, no training):** extraction repair pass;
  open-ended "other material terms" extraction (any contract type); company-neutral data-derived
  materiality rubric; supplements/gap-fill UX; registrant profile as an editable admin input
  (`registrant.json` + Company web tab); auto-detect triggered Items on upload (suggest+confirm,
  by document role); **multi-document filing** (contract + press release + …, user routes each
  doc→Item; news Items 7.01/8.01 furnish the press release as Exhibit 99.1; merged exhibit index);
  `.txt` input.
- **NEXT:** #4 — per-customer **few-shot style** (facts-stripped exemplars from the customer's own
  past 8-Ks) so drafts read like that filer wrote them; then deepen extraction. NO fine-tuning.
- **Run:** LLM = Docker `lawrag-llm` on :8012; web = manual `./.venv/bin/python scripts/serve.py`
  on :8080 (restart + HTTP-verify after any `lawrag/*.py` edit). Full design report:
  `8K_DRAFTING_FINDINGS_REPORT.md`.
- Sections below this are the DATED HISTORY of how we got here (adapter/delex era included) —
  context, not current instructions.

## What this is
Fully-local, private RAG + drafting system for Richtech's legal counsel, on a Jetson
AGX Thor. The **8-K drafting** tool turns a contract (+ optional supplements) into an SEC Form
8-K, grounded in the source documents. Goal: commercialize as a per-company product (shared base
model + per-customer style via few-shot/rubric, NOT per-customer fine-tuning). An earlier
fine-tuned adapter + delex experiment was tried and RETIRED (see CURRENT STATE).

## Current phase — 8-K LoRA adapter, VALIDATED
- Built a training corpus from **public EDGAR** filings (~90 companies, all 8-K Items):
  `data/multico_all/` (gitignored) → **2,174 "source doc → real disclosure" pairs**
  (`train_pairs.jsonl`), split by company. Contract-family clean core ≈ 552.
- Shipped a self-contained `training/` package (pushed to GitHub) and **trained the
  adapter on an in-house RTX PRO 6000 (3× bf16 LoRA via LLaMA-Factory)**.
- **A/B result (held-out companies, adapter OFF vs ON) — clear win:**
  ROUGE-L 0.246 → **0.464** (~+89%); number-recall 0.577 → **0.675**; avg output
  2430 → **1098 chars** (tightens to real-filing length). Base rambles/emits `<think>`;
  adapter goes straight to a tight, correctly-formatted disclosure.
- Adapter is in the repo: `training/adapter-8k-v2/` (bf16 LoRA, r32/α64, targets
  q,k,v,o,gate,up,down_proj — the "safe" set; v1's `lora_target=all` hit the hybrid
  model's `linear_attn` SSM projections + `shared_expert_gate` and collapsed
  generation — do NOT use `all`).
- **This is an interim run (v2 on 2,174 pairs), NOT the final training** — step A
  (scale to ~3,000+ pairs → v3) is still pending.

## C — human/legal quality check DONE (2026-07-10): STYLE win, FACT red line hit
Reviewed the 6 samples in `training/eval_samples_lf.txt` (AAPL 7.01×2, 5.02×2; KSCP
1.01, 2.01) — adapter OFF vs ON vs REAL filing. Verdict: **adapter is a clear win on
style/structure but FABRICATES specific figures on number-dense disclosures — the
exact compliance red line we designed the RAG pipeline to avoid.**
- ✅ **Style/structure**: AAPL 7.01 Reg-FD furnishing legend is textbook-correct;
  tight 1.01 framing + qualifier; no `<think>`/placeholder leaks (base emitted both).
- ❌ **Fact fabrication on complex filings**:
  - **KSCP Item 1.01 (OUR CORE ITEM)** — re-invented the whole consideration
    structure: deferred payment written as "$1M ×4 in 2027" (REAL = $500k/qtr ×8 over
    2027–2028); earn-out "$1M in installments" (REAL = up to $2M tied to 2026
    revenue/margin); cash/equity revenue-share figures all wrong; **dropped the $1.1M
    Frost Bank debt assumption**. Base actually read the deferred schedule correctly —
    so the info WAS in the source; the style-LoRA distorted it.
  - **AAPL 5.02 (2022 stock plan)** — share count wrong (500M vs real 510M + formula
    cap 1,274,374,682), invented an evergreen auto-increase clause, then degenerated
    into a repetitive "whereas the 2014 Plan…" hallucination loop.
  - **AAPL 5.02 (cash incentive plan)** — not fabricated but ran long and **truncated
    at max_tokens** mid-sentence.
- **Mechanical `_compliance_flags` on the KSCP 1.01 adapter text = 5/6 PASS** yet
  misses every fabricated number — presence checks CANNOT catch format-correct-but-
  wrong figures. Important limitation to remember.
- **More data (v3) will help style, length calibration, and the degenerate-loop
  problem, but will NOT fix number fidelity** — that's structural to using generative
  weights for facts (number-recall 0.577→0.675 = still ~⅓ wrong). Zero-tolerance for
  8-K numbers ⇒ facts MUST stay in the RAG/extract + verbatim-quote-verify layer.
- **Product implication for deploy (B): adapter = STYLE layer only.** Either (A,
  recommended) keep `draft.py`'s RAG extraction + `verify_quote` as the fact source and
  use the adapter only to polish phrasing/structure, or (B) if the adapter drafts
  directly, add a number-level reconciliation pass (every amount/share/date matched
  against the quote-verified extracted facts, flag mismatches). Do NOT ship the adapter
  as a standalone fact source. Also raise max_tokens / add a "summarize-only" constraint
  for plan-type Items (5.02) to stop truncation.

## v3 negative result + fact GUARDRAIL shipped (2026-07-10)
- **v3 (data-cleaning) trained on the RTX box did NOT beat v2** — aggregate metrics
  ~flat (number-recall slightly *lower*, output shorter), still fabricated on the clean
  held-out AAPL 5.02. This **empirically confirms fabrication is structural**, not a
  data-volume/quality problem. Decision: **v2 (`training/adapter-8k-v2/`) is the final
  style adapter; no more fidelity training.** Useful negative result — it vindicates
  "adapter = style, facts = a separate guardrail". See [[guardrail-red-only]].
- **Fact-fidelity guardrail built + wired in** (`lawrag/guardrail.py`, spec
  `training/GUARDRAIL_SPEC.md`, tests `tests/test_guardrail.py` 10/10). Normalizes then
  reconciles every material datum (currency/count/percent/date/party) in a DRAFT against
  the SOURCE contract. Pure local text — NO DB/embedding/retrieval (orthogonal to the
  vector stack; "no RAG" ≠ "no fact-check").
  - **Verdict is RED-only** (spec §4, amended by the spec owner 2026-07-10): RED =
    fabrication (incl. model-COMPUTED figures, e.g. a $960k OID not stated verbatim) =
    the only status that blocks. Omission = AMBER = review-only, never blocks, NOT in
    the verdict. A blanket omission check produced 39 noise flags on a 2.03 note (8-K
    disclosure is deliberately selective), so AMBER is scoped via `reconcile(...,
    must_disclose=<keywords>)` to rubric MUST-disclose fields. Default = **RED-only
    shipped now (Option A)**; **scoped AMBER (Option B) PENDING** the rubric→keyword map.
  - **Wired**: `draft_8k()` attaches `result["_guardrail"]`; review pack (Word/PDF)
    renders a "Fact reconciliation" section; web shows a one-line verdict banner
    (details stay in the downloadable review pack). Verified E2E over HTTP.
  - `scripts/serve.py` now self-bootstraps `sys.path` — start with
    `./.venv/bin/python scripts/serve.py` (run via the harness background mechanism, not
    shell `&`: the sandbox kills shell-backgrounded procs and drops PYTHONPATH).

## B — DEPLOYED ON THOR, RTX-FREE (2026-07-13)
v2 8-K adapter is live and serving **100% locally on Thor** — no RTX dependency for
inference. Full chain verified end-to-end.
- RTX (its last GPU jobs before decommission) merged bf16 base+adapter and **quantized to
  NVFP4** (21GB, `adapter-8k-v2-nvfp4/`), plus a `tight_template.jinja` (empty
  `<think></think>` → no reasoning preamble). GGUF Q4_K_M (~20GB) exists as a portable
  llama.cpp fallback but loses `guided_json`, so we use NVFP4+vLLM.
- Transferred 21GB RTX→Thor over Tailscale via `rsync` (SSH_ASKPASS one-time password;
  no persistent key). Model at `/home/jetson/models/qwen36-8k-nvfp4` (OUTSIDE the repo).
- Serving: the LLM runs as a **Docker container** (`nvcr.io/nvidia/vllm:26.05-py3`,
  `--runtime nvidia --network host`), NOT bare vllm. Swapped `lawrag-llm` → `lawrag-llm-8k`
  (old kept Exited for rollback: `docker start lawrag-llm`). `.env` `LLM_MODEL=qwen3.6-8k`.
  **Exact cmd + the `processor_config.json` crash-loop gotcha are in `DEPLOY_THOR.md`.**
- **Verified E2E**: served `qwen3.6-8k` on :8012, no `<think>` leak, `guided_json`
  structured output works through `draft_8k`, and the fact guardrail correctly BLOCKED the
  model's fabricated figures (RED) — the deploy proves adapter(style)+guardrail(facts).
- Remaining tidy-ups: RTX can now be decommissioned (delete its 66GB bf16 master / 69GB
  bf16 GGUF once satisfied); optionally `docker rm lawrag-llm` after a few days' confidence.

## Post-deploy hardening + multi-Item (2026-07-13/14)
Shook out on the live adapter against a real Richtech SPA (accession 0001213900-26-009823):
- **Structured-output truncation fixed** (`Unterminated string` on generate): the adapter
  is far more verbose than the base and overran `max_tokens`, truncating guided-JSON
  mid-string. Bounded the output — `maxLength`/`maxItems` on `REVIEW_SCHEMA` (extraction)
  and `DRAFT_SCHEMA` (drafting) + terseness in the extraction prompt + drafting cap
  4096→8192. Both extraction and drafting now complete within the 32k context.
- **Precedent fact-leakage fixed → `draft_8k` `n_precedents` now defaults to 0.** The
  in-prompt precedents were redundant (style is in the adapter's weights) and the model
  copied their FACTS (share counts, file numbers, an S-3-registered story) into the draft,
  contradicting the source contract. Off by default kills the leak and means pure
  generation needs no DB/retrieval. Confirmed: leak gone, correctly reads the deal as a
  private placement.
- **Residual figure fabrication is the inherent RAG limit, not a bug**: this SPA states
  `$4.55/share` + `$38,675,000` but NOT a total share count (8,500,000 is derivable, not
  written) — so the model invents a share count. The **guardrail flags it RED for human
  fill**; no training fixes this. Workflow = tool gives a grounded skeleton, the guardrail's
  RED list tells counsel which figures to supply/confirm.
- **Multi-Item drafting (Plan A) shipped** — real 8-Ks bundle several Items. `draft.draft_filing(contract, items)`
  drafts substantive Items from the contract and auto-fills recognized cross-reference
  Items (3.02→1.01, 2.01/2.03→1.01) with the "incorporated by reference" boilerplate
  (no LLM). Result carries the primary Item at top level + `_items[]` (ordered sections);
  guardrails merged. Web Generate tab is now multi-select checkboxes; `/api/generate/8k`
  takes `items` (comma-sep); export (Word/PDF) renders every section. Verified E2E:
  `1.01,3.02` → filing with both Items. **Item 8.01 (press releases) still needs those
  docs as input = Plan B (not built).**

## Grounded securities figures + anchored derivation (2026-07-14)
Shaken out on the real SPA (0001213900-26-009823), which states $4.55/share +
$38,675,000 aggregate but NO total share count:
- **1.01 checklist** gained "Securities Type/Class (and par value)", "Number of Shares
  or Units Issued", "Price per Share/Unit" → $4.55 and $0.0001 are now extracted and
  used (were fabricated $2.00/$0.001). `_SYSTEM` rule 5 forbids computing/deriving figures.
- **Anchored derivation (guardrail):** `draft._derive_share_count` computes the missing
  count deterministically (aggregate ÷ per-share, from labeled clauses) and passes that
  ONE value to `guardrail.reconcile(..., derived=[...])`. A draft figure matching it is
  "derived" — grounded, review-required, NON-blocking, arithmetic shown. Verdict now
  {blocked | needs_review | clean}. **Safety:** the guardrail does NOT blind-search
  number pairs (an earlier version coincidentally "grounded" a wrong 1,000,000 =
  $50,000 × $20) — only the passed-in labeled value is honored; wrong figures stay RED.
  Locked by tests (11/11).
- **Honest limit re-confirmed:** on this SPA the model confabulates the numbers anyway
  (invents count/aggregate/dates, ignores the supplied 8,500,000) → still correctly
  BLOCKED. Derivation only helps when the model states the anchored value; it cannot
  rescue a mangled draft. The guardrail (detect → human fix) remains the guarantee; the
  model is never trusted for facts.
- **In-app edit + re-verify (this is the answer to "how do I clear the banner"):** each
  substantive Item disclosure is editable in the web view; a "Re-check facts" button POSTs
  the edited text to `POST /api/generations/{id}/reverify`, which re-runs the guardrail
  against the stored `_source_text` and re-saves → the banner updates live. `draft_8k`
  now stores `_source_text` + `_derived_values` so no re-parse is needed. The guardrail
  also grounds a figure derived from two figures that are BOTH source-matched AND stated
  in the draft (`guardrail._derive_from_grounded`, transparent/low-coincidence). Verified:
  blocked draft → edit to correct figures → re-check → `needs_review` with 8,500,000 shown
  as `derived = $38,675,000 ÷ $4.55` (no longer blocked). So the lawyer fixes the flagged
  cell and clears the block in-app.

## Fact-locked drafting — zero imagination (2026-07-14)
User's hard requirement: "we can't allow so many imagination, NEVER." The 8-K adapter,
even precedent-free, confabulated whole narratives on the real SPA (invented share
count, an S-3 registered-offering story + fake dates leaked from the fine-tune's TRAINING
memory of Richtech's own past filings). So drafting is no longer LLM-written:
- **`draft_8k(fact_locked=True)` (now the default)** assembles the disclosure
  DETERMINISTICALLY from the verified extracted clauses (+ the code-derived share count)
  via `draft._assemble_disclosure` — the model NEVER writes a figure, so it cannot imagine
  one. No LLM drafting call. Every stated fact is cited to its verbatim source quote;
  facts the extraction didn't capture are OMITTED, never invented. `fact_locked=False`
  keeps the old LLM-drafting path for comparison.
- Verified on the real SPA: guardrail **CLEAN, zero fabrication**, correct qualifier +
  (c) material-relationship statement.
- **Tradeoff (by design):** prose is templated and THIN where the extraction missed a
  field (e.g. it got $4.55/share + $0.0001 par but not the aggregate that run, and
  doc_type came out as junk "SPG" → generic "definitive agreement"). It errs toward
  OMISSION, never invention. **Next lever for fuller auto-drafts = extraction
  completeness/reliability** (reliably capture the aggregate, a clean agreement name),
  NOT trusting the model to write facts.

## Drafting modes finalized — HYBRID is the default (2026-07-14, user chose "B")
`draft_8k(mode=...)` now has three modes (replacing the earlier `fact_locked` bool):
- **`hybrid` (DEFAULT):** model drafts in its 8-K style → `draft._lock_figures` replaces
  every figure NOT grounded in source (or a valid derivation) with `[NOT IN SOURCE —
  CONFIRM]`. No imagined NUMBER survives; `result._blanked_figures` lists them; web banner
  + re-verify surface/track them. Keeps fluent prose. **LIMIT: locks numbers only** — a
  non-numeric fabrication (e.g. the model's "registered direct offering / Form S-3 (File
  No. 333-286333)" story, or an invented warrants paragraph) still needs human review.
- **`assemble`:** deterministic assembly from verified facts; model writes no prose →
  ZERO invention (number or narrative), but templated/thin. The only fully-safe-against-
  narrative option.
- **`llm`:** raw, legacy.
Also fixed the (c) material-relationship counterparty to use the EXTRACTED party (was
mis-picking "the SEC" from the model's defined terms). Note for future: the standing
tension is hybrid(nice prose, numbers safe, narrative not) vs assemble(safe, thin) — the
deep fix for both is extraction completeness + (later) a narrative-claim check.

## v4 delexicalized adapter — deployed, backfill built, NEEDS v5 before production (2026-07-15)
RTX built **v4**: trained on DELEXICALIZED pairs (deal facts → typed placeholders
[AMOUNT_n]/[NUM_n]/[PCT_n]/[DATE_n]/[ORG_n]/[PERSON_n]), so the model learns 8-K
structure/style only and **structurally cannot emit a real number** — it outputs a
placeholder skeleton; the Jetson backfills real values from the source. This is the real
answer to "NEVER imagination" (numbers come from deterministic backfill, not the model).
- **Deployed on Thor:** NVFP4 (21GB) at `/home/jetson/models/qwen36-8k-v4-nvfp4`, served
  as Docker container `lawrag-llm-8k-v4` on **:8013** (`served-model-name qwen3.6-8k-v4`,
  tight template). Config: `CONFIG.llm_v4_base_url`/`llm_v4_model`.
- **Backfill built + validated:** `lawrag/delex_backfill.py` (reuses `training/llamafactory/delex.py`
  — spaCy + en_core_web_sm installed on the venv). Round-trips verbatim; hallucinated/
  off-by-index placeholders → `[NOT IN SOURCE — CONFIRM]` + guardrail RED. Guardrail
  broadened to catch ≥4-digit comma numbers (v4's occasional literal invention, e.g.
  "10,000 sq ft" when real is 79,325). `draft_8k(mode="delex")` = parse (no LLM) → delex →
  v4 → backfill → guardrail; needs ONLY v4 (no extraction model / no 2-model GPU contention).
- **BLOCKER found — placeholder-numbering misalignment (RTX's flagged risk, confirmed):**
  slot-alignment test at a larger (60k) source window fixed the literal number invention
  (real 79,325 captured) BUT v4 backfilled the **WRONG orgs** into Company/Seller slots
  ("Title Company" as the Company, "EBS Realty Partners" as seller). Root cause: training
  numbered placeholders **output-first**; inference numbers **input-only** — with many
  entities the indices drift. **These wrong values ARE real orgs in the source, so the
  guardrail CANNOT catch it** (張冠李戴). So v4 is NOT production-safe yet.
- **INTERIM (now):** v2 restored as the working model (guardrail catches its number
  fabrication — safer than v4's wrong-orgs); `draft_8k` default back to `mode="hybrid"`;
  v4 container stopped. **`docker start lawrag-llm-8k` = v2; do not run v2+v4 together at
  0.5 gpu-util (won't fit).**
- **NEXT — RTX training v5 now (input-first + 24k window):** RTX confirmed two hard walls
  that kill the naive "80k window" idea: (1) **data** — the training corpus source docs were
  hard-truncated at **24000 chars** (935/2174 hit exactly 24k; full EDGAR docs up to ~400k
  but cut at packaging), so an 80k *training* window needs a full-corpus rebuild (big); (2)
  **GPU** — 80k≈22k tokens ≈ 3× the current 8192 cutoff, and v4 already fills 96GB at 8192
  (DDP, 70GB base/card) → would need DeepSpeed ZeRO-3. **But the confirmed blocker (wrong-org
  slots) does NOT need a big window** — parties are in the contract's opening (<24k). So v5 =
  **input-first numbering + 24k window** (uses existing data, fixes the misalignment, fits
  current DDP, ~2h). Training/inference windows are INDEPENDENT: Jetson still feeds the FULL
  contract at inference; the open question is whether a ≤24k-trained model generalizes to
  copy placeholders that appear DEEP (>24k) in the input.
- **v5 re-test plan (Jetson, at full inference window):** ① **party/org slot alignment** —
  should be FIXED (the confirmed blocker); ② **deep-fact coverage** — can it copy a `[NUM]`
  at ~60k chars (e.g. the 79,325 sq ft), or does it miss/invent (guardrail backstops
  invention)? Expectation: ① fixed; ② likely limited by the 24k training length (24k≈6k
  tokens; positions beyond that are past what it trained on) — MEASURE it. Only if ② is
  inadequate → **v6** = rebuild full-text corpus + ZeRO-3 long-context (do NOT pre-invest).
- If v5 passes ① (and ② is acceptable) → switch to v5, `mode="delex"` default, retire v2.
  v2 was only ever the 8-K *style* model; obsolete once a delex adapter aligns (extraction/
  DD, if ever needed, use the plain base, never v2).
- **[SUPERSEDED 2026-07-16 — the "80k window / long-context" idea is DEAD. Measurement
  below ("WINDOW IS NOT THE BOTTLENECK") shows widening the window buys only +5 pts; the
  fix is delex-quality + corpus filtering at the current 24k, not a longer window.]**

## v5/window investigation — WINDOW IS NOT THE BOTTLENECK (2026-07-16)
RTX inferred the residual delex misalignment (canon fix only moved it 39→42.4%) was
the 24k source truncation, and proposed full-text corpus + ZeRO-3 long-context (v6).
Jetson tested that premise directly (raw full docs ARE on disk, `data/multico_all/`,
max 399,888 chars). **The premise is DISPROVEN — widening the training window barely
helps.**
- **Rebuilt full-length pairs** non-destructively: `build_training_pairs.py` now reads
  `MAX_INPUT_CHARS`/`PAIRS_OUT` from env; ran at 120k →
  `data/multico_all/train_pairs_full.jsonl` (input median 17,564 / max 399,888).
- **Measured placeholder alignment** (`scratchpad/measure_align.py`: fraction of OUTPUT
  placeholders whose canonical `(type, value)` also appears in the INPUT window — i.e.
  the model has a source anchor to copy/backfill). Micro over 297 stratified pairs:
  - Window **15k→24k→48k→120k = 34.9% → 36.7% → 38.6% → 39.9%** — full text buys only
    **+5 pts**. Core 1.01: 27.8%→36.3%. Median doc is 17.5k, so the full source is
    already in view; the missing facts are NOT beyond the window.
  - **canon (value-aware) lever** (`measure_align2.py`): `$38.7M`↔`$38,675,000` unit
    expand + ±1% fuzzy + date→(y,m,d). AMOUNT **32.6%→51.8% (+19)**, NUM +8, overall
    39.9%→**45.2%**. Cheap (edit `delex.py`), ~same total gain as the whole window.
  - **ORG 33% / PERSON 25% untouched by either** (37% of all placeholders). Probe
    (`probe_orgper.py`): part is a fixable **greedy ORG regex** (matches cross-sentence
    junk like `'On\nAugust 20, 2024, Solid Power Operating, Inc.'`), but a real chunk is
    **structurally absent** — disclosures cite facts NOT in the paired exhibit (director
    bios' past employers "Ophir Corporation"/"Ball Aerospace", officer names in a plan
    doc, rounded/paraphrased amounts). No window/canon fixes those.
- **Conclusion:** the delex-copy ceiling is ~55% even with all cheap fixes, because real
  8-K disclosures transform/round facts and pull from outside the paired source. **Do NOT
  invest in ZeRO-3 long-context (v6) — window ≠ bottleneck.** RTX's ZeRO-3 smoke test is
  fine as an infra capability check; just don't build the full-text training on it.
- **Revised v5 plan (cheap, Jetson-side, better than naive-v5 AND ZeRO-3):**
  1. Fix delex quality — greedy ORG regex, value-aware numeric canon, date canon.
  2. **Filter the corpus to groundable pairs** (keep only pairs whose output placeholders
     are ≥~85% anchored in the input) so supervision is consistent = "only emit
     placeholders you can copy" (directly kills the ungroundable-placeholder → wrong-org
     failure). Measure the KEPT subset's alignment.
  3. Retrain v5 at the **current 24k window** + input-first numbering on the cleaned,
     filtered corpus. If kept-subset alignment ≥85% → v5 is trainable & safe; dropped
     pairs are simply not delex-suitable (route them to guardrail/human).
- Interim unchanged: **v2 in production** (`docker start lawrag-llm-8k`, `mode="hybrid"`),
  guardrail catches fabrication. Scratchpad measure scripts are EPHEMERAL — logic is
  captured here + will fold into `training/llamafactory/delex.py` when v5 is built.

## ARCHITECTURE DECIDED + extraction quality lever shipped (2026-07-16)
After a full strategic review of v1→v5 (see the bilingual **`8K_DRAFTING_FINDINGS_REPORT.md`**),
the production architecture is settled and the first quality improvement is built.
- **Decision:** the spine is **`draft_8k(mode="hybrid")` = v1 (model drafts on RAG-extracted
  facts) + guardrail** (already the code default). Reframe: **model UNDERSTANDS/extracts, code
  GENERATES; facts never come from model weights.** `assemble` = optional max-safety mode, NOT
  default. **v2 adapter + delex/v5 = optional layers, NOT the core; no further model training on
  the critical path.** The remaining gap is an INFORMATION problem (facts absent from the source),
  addressed by extraction completeness + human supplements — not by training.
  - v1 tested fact-clean; v2→v5 chased human-like STYLE only. Style is obtainable WITHOUT
    fine-tuning: deterministic EDGAR export (structure) + prompt/rubric (tone) + facts-stripped
    few-shot from the customer's filings.
- **SWITCHED PRODUCTION TO THE BASE MODEL (2026-07-16).** `:8012` now serves the plain base
  `unsloth/Qwen3.6-35B-A3B-NVFP4` (served-name `qwen3.6`, container `lawrag-llm`); `.env`
  `LLM_MODEL=qwen3.6`. v2 adapter container `lawrag-llm-8k` STOPPED but kept for rollback
  (`docker start lawrag-llm-8k` + set `.env` back to `qwen3.6-8k`; `.env.bak-8k` is the old env).
  **A/B on the real 2025-04-14 PSA — base BEATS the v2 adapter on the accuracy-first spine:**
  extraction filled 17/26 vs 16, **verified 17/17 vs 15/16**, needed 1 repair vs 4; hybrid draft
  guardrail CLEAN for both, base has NO `<think>` leak (guided_json constrains it) and lower
  narrative-fabrication risk. Only ONE model fits at 0.5 gpu-util, so this is a swap not a
  co-serve. Extraction also uses `:8012`, so base improves extraction too.
- **#1 lever shipped — verify-gated extraction repair** (`summarize._repair_extraction`, wired
  into `review_contract`): after the first pass, clauses with a value whose quote FAILED
  verification, or still 'Not found', get a targeted 2nd pass; a repair is accepted ONLY if the
  new quote verifies against the source → improves fidelity AND completeness, never invents.
  Also added map-reduce **window overlap** for long docs. `_repaired` count surfaced on the
  review + draft result.
- **Verified E2E** on the real 2025-04-14 PSA (1.01, L&R Investment / 2975 Lincoln Rd / $4.1M):
  extraction filled 16/26, **verified 15/16, repair fixed 4**; `draft_8k` hybrid →
  **guardrail CLEAN, 0 blanked figures, all 6 compliance checks pass**, fluent EDGAR-style
  disclosure with every number (incl. 20,200 sq ft, 1.26 acres, 5.0% commission) grounded.
  Web server restarted + HTTP-verified after the `lawrag/*.py` edits.
- **#2 lever shipped — supplements / gap-fill UX.** Each `[NOT IN SOURCE — CONFIRM]` blank the
  guardrail left is now a labelled input (with the surrounding sentence for context) in a "Fill
  the flagged gaps" web panel (`web/app.js` + `.gap-*` CSS). `POST /api/generations/{id}/supplements`
  substitutes the reviewer's value into the disclosure AND records it in `_derived_values` so the
  guardrail treats it as GROUNDED (like a derivation: review-required, non-blocking) instead of
  re-flagging it as fabricated. `reverify` + supplements now share `_recompute_verification`.
  Verified: logic (fill → verdict needs_review, blanks 0), route (401 unauth / 200 authed), and a
  full HTTP login→generate→(clean)→delete E2E on the base model.
- **De-biased the materiality rubric (Task A, 2026-07-16).** The 1.01 `ITEM_RULES` rubric was
  DERIVED FROM 17 Richtech filings ("Richtech's counsel rarely states X", "0 of 10 filings")
  — a single-issuer bias unfit for a multi-company product. Replaced with **company-neutral
  general 8-K materiality guidance** (same SEC (a)-(d) requirements + a general "include the
  material commercial terms, fold boilerplate into 'customary provisions', err toward
  inclusion" section); removed Richtech-specific hit-rates/deal-type habits from `ITEM_RULES`,
  the comment block, and the `_SYSTEM` redaction example. **Verified on the L&R PSA: facts still
  guardrail-CLEAN, and the draft is now MORE complete — it includes closing timing, termination
  rights, and the customary-provisions catch-all the biased rubric had suppressed** (matching
  what the real filing did). A customer's own style is planned to live in a facts-stripped
  few-shot layer (#4), NOT in the materiality rules.
- **Data-derived GENERAL rubric shipped (Task B, 2026-07-16).** `training/build_general_rubric.py`
  measures market-norm disclosure rates across the multi-company corpus (deterministic keyword
  scan, no LLM, deal-type aware). Baked the measured 1.01 bands into `ITEM_RULES` replacing Task
  A's prose: across **245 real Item 1.01 disclosures (~90 issuers)** — price 89% (ALWAYS);
  term 60%/asset 55%/closing 46%/conversion 37%/reps 53% (USUALLY, deal-type dependent: debt →
  interest 58%/conversion 57%; equity → reps 87%/closing 68%); **governing law 0/245, dispute
  resolution 0.8%, confidentiality 6%, assignment 10% (fold into 'customary provisions')**.
  Kept earnest money as "include when present" (2% overall is only because few corpus deals are
  real estate — not a signal to omit it). Verified on the L&R PSA: guardrail CLEAN, complete
  draft. (2.03/3.02 band data is printed too, for when their ITEM_RULES are added.)
- **Narrative-claim audit shipped (Task #6, 2026-07-16/17).** The numeric guardrail locks
  fabricated FIGURES, but a model can still invent a non-numeric claim or a SPELLED-OUT number
  (found live on the ATM contract: an invented "terminate upon **ten** business days' notice" —
  the contract says termination is "at any time"; the numeric guardrail missed it because "ten"
  is a word). `draft._narrative_flags` runs an LLM audit of each substantive draft sentence
  against the **grounded facts** (the extracted, quote-verified clauses — NOT the raw contract,
  which avoids long-doc windowing false-positives) and flags unsupported claims. REVIEW-ONLY:
  never blocks, never alters the draft — purely additive. Skips boilerplate ((c)/qualifier/FLS).
  Wired into `draft_8k` (hybrid/llm) + recomputed on reverify/supplements (evidence = stored
  `_grounded_facts` + reviewer supplements + business context, so confirmed facts aren't
  re-flagged); surfaced in the web banner (`_narrative_flags`). Verified: flags the invented
  termination-notice, does NOT false-flag the real exclusivity term or the 3.0% fee.
- **Also this session:** open-ended extraction (long-tail material terms, any contract type) +
  drafting nudge to include them; party/instrument robustness (`_instrument_noun` prefers a real
  instrument word so a party role like "Agents" is never taken as the instrument; `_PARTY_TERMS`
  broadened; `_clean_party` strips trailing "(the \"X\")"); (c)-counterparty picks the
  non-registrant party. ATM held-out test: exclusivity now captured+drafted, (c)/qualifier
  correct. **Honest boundary:** self-contained contracts (real-estate PSA) → near-publishable;
  registered securities offerings (ATM) → correct-but-incomplete (offering size, S-3 file no.,
  5.1 opinion / 23.1 consent live OUTSIDE the single agreement → supplements/multi-doc).
- **Registrant profile de-hardcoded (commercialization, 2026-07-17).** The 8-K cover/signature
  registrant was a hardcoded `export.REGISTRANT` dict (Richtech) — unfit for a multi-company
  product and the source of the stale-cover-address issue. Now `export.load_registrant()` reads
  a per-deployment **`registrant.json`** (path via `CONFIG.registrant_file` / `REGISTRANT_FILE`
  env), merged over a built-in default so nothing breaks if absent. A new customer / a changed
  address / new officers = a config edit, no code change. `registrant.json` is gitignored (like
  `.env`); `_DEFAULT_REGISTRANT` remains the fallback. Loaded at import → edit then restart the
  server. Verified: editing the file changes the rendered address. (Competitor CaseMark takes
  registrant identifiers as an input too — this matches; see [[casemark-competitor]].)
- **Registrant now editable IN-BROWSER (admin, 2026-07-17).** New **Company** admin tab
  (`view-company`) + `GET/PUT /api/registrant` (require_admin) edit the profile via a form
  (name/state/File No./EIN/address/phone/securities rows/EGC/signer) → writes `registrant.json`
  via `export.save_registrant`. Exports now call `load_registrant()` at RENDER time (not the
  cached module `REGISTRANT`), so an edit applies to new drafts with **no restart**. Verified
  E2E over HTTP: admin GET/PUT 200 + persisted; non-admin 403. This is CaseMark's
  "registrant as input" done as a first-class admin screen.
- **Auto-detect triggered Items shipped (2026-07-17).** On upload, `draft.detect_items` (LLM
  classifier over the doc head, `POST /api/detect-items`) suggests which 8-K Item(s) the
  document triggers, with a one-line reason; the Generate tab now **detects → pre-checks the
  suggested boxes + shows reasons → user confirms/adjusts → Generate** (was drop=immediate
  draft). SUGGESTION-ONLY (never auto-commits). Conservative prompt explicitly forbids 2.01
  unless the deal has CLOSED — so it AVOIDS CaseMark's error (CaseMark wrongly reported a 2.01
  completion for an unclosed PSA). Verified: PSA→1.01 (not 2.01), ATM→1.01, convertible note→2.03.
- **Multi-document filing — BACKEND SKELETON built (#5, 2026-07-17).** `draft_filing` now takes
  ONE OR MORE source docs (single path still works). `_route_items` maps each Item to the doc
  that triggers it (contract→1.01/2.03/…, press release→8.01); NEWS Items 7.01/8.01 added to
  `ITEM_TITLES` with a summarize-the-press-release path (`_draft_news`) that furnishes it as
  Exhibit 99.1 (guardrail + narrative audit still run against that source). `_build_exhibits`
  merges the 9.01 index (10.1 + 99.1 + 104); result carries `_exhibits`. Contract-only
  post-processing ((c) statement, 10.1 qualifier) is gated to `CONTRACT_ITEMS`. Verified
  structurally (contract + press release → 1.01 + 8.01 + merged exhibits, guardrail clean).
  **Caveats / still TODO:** (a) auto-routing of press-release→8.01 isn't reliable yet (in the
  test an unrelated press release wasn't tagged 8.01 so it fell back to the contract) — improve
  detect for news docs OR let the user assign doc→Item in the UI; (b) API/web still single-file
  — need multi-file upload + render `_exhibits` in export/web; (c) 7.01 furnish nuance / multiple
  99.x numbering. Backend is the skeleton; delivery plumbing is next.
- **Multi-document DELIVERY shipped (#5, 2026-07-17).** End-to-end usable now:
  - `POST /api/generate/8k` takes **multiple `files`** + an `assignments` JSON
    (`[{filename, items:[…]}]`) — the user's confirmed doc→Item mapping; `draft_filing` accepts
    an explicit `routing` (UI wins over auto-detect). Legacy single-file still works.
  - Web Generate tab (**choice B**): drop one or more docs → each becomes a **card** whose
    detected Item(s) are pre-checked, the user confirms/adjusts which Item(s) that doc covers →
    Generate. Per-file cards replace the old global checkboxes.
  - `_exhibits` (10.1 + 99.1 + 104) rendered in the web view AND both exporters (`_exhibit_rows`
    in export.py; Word table row-count now dynamic; HTML table row list dynamic).
  - **`.txt` now a supported input** (`parsers.parse_text`) — press releases are often plain text.
  - Verified E2E over HTTP: contract(pdf, 1.01) + press release(txt, 8.01) → filing with both
    Items, **8.01 drafted from the press release** (correct routing via user assignment), exhibits
    10.1+99.1+104, guardrail CLEAN, 8.01 furnishes Exhibit 99.1. Each Item still runs the numeric
    guardrail + #6 narrative audit against ITS source.
- **Multi-doc tuning from a real held-out test (2024-09-05 SPA + press release, 2026-07-17).**
  Compared against the real filing (Items 1.01 + 8.01 + Exhibits 10.1/99.1/99.2/104):
  - **Detection now classifies by DOCUMENT ROLE:** a press release → 8.01 (or 7.01), NEVER the
    substantive Item it merely discusses (was mis-tagging the offering press release as 3.02 →
    press release wasted, no 99.1). Fixed in `_ITEM_DETECT_SYSTEM`. Now: SPA→1.01, press
    release→8.01, exhibits 10.1+99.1+104 — matches the real filing's structure.
  - **Narrative audit de-noised:** was firing 8 flags (mostly duplicates + framing/boilerplate).
    Now dedupes by claim, re-merges sentences split at abbreviations ("…Inc." | "(the Company)…"),
    skips framing/boilerplate ("entered into", "customary …", "furnished as"), and checks against
    grounded facts **+ the raw source** (so a real fact the checklist missed isn't false-flagged).
    Result on the same draft: 8 → 1 flag (the one substantive $-figure sentence — a useful
    "verify" prompt, not noise).
  - Honest limit reconfirmed: our 1.01 from the *Form of* SPA is terser than the real 1.01
    (share counts / placement agent / net proceeds / S-1 file no. live in the prospectus, not the
    form) — that's the multi-doc/supplement gap, not a bug.
- **Next roadmap levers (no training):** #4 tone via facts-stripped few-shot from the customer's
  own filings; deepen extraction; offering-specific exhibits (5.1/23.1) as reviewer supplements;
  #7 free base-model upgrades.

## Three quality fixes from a real held-out PSA review (2026-07-20)
Reviewed a fresh generation on the real 2026-04-01 EBS Rainbow PSA (accession 0001213900-26-041153,
$21.18M, 79,325 sq ft; guardrail CLEAN, near-publishable). Found + fixed three defects:
- **Duplicate qualifier paragraph** (`draft._ensure_exhibit_qualifier`): the de-dup guard tested
  for the exact string `"qualified in its entirety"`, but the model sometimes drops "its"
  ("qualified in entirety") → guard misses → a second qualifier is appended. Fix: match
  `qualified in (?:its )?entirety` and normalize "in entirety" → "in its entirety". Unit-verified
  (2 paras → 1).
- **Narrative audit false positives** (`draft._narrative_flags`): the single batch "find the
  unsupported claims" call has high recall but poor precision — it flagged ALL 4 substantive
  sentences of the clean draft even though every fact quote-verifies (issue field = whole sentence
  = not real analysis). Measured: the SAME model, asked ONE isolated claim at a time, was 4/4
  correct. Fix: added a **confirmation pass** — each batch-flagged claim is independently
  re-checked (`_NARRATIVE_CONFIRM_SYSTEM/_SCHEMA`) and DROPPED if found supported; review-only so
  a failed check keeps the flag. Verified on the real draft: 4 → 0 false positives, and an injected
  fabrication ("terminate on ten business days' notice" vs the contract's "at any time") is still
  KEPT. This is the adversarial-verify pattern (batch = recall, per-claim = precision).
- **`<think>` reasoning leaked into the grounded facts / review pack** (`summarize._merge`): on
  long (map-reduced, >90k-char) docs the reduce-summary used plain `llm.chat`, so the base model
  wrote its "Here's a thinking process: 1. Analyze… 4. Check Constraints" preamble straight into
  the "Contract summary" field (no `<think>` tags, so a tag-strip wouldn't catch it). Fix: route
  the reduce through `chat_json` with `_SUMMARY_SCHEMA` (guided JSON can't emit a preamble), like
  extraction already does. Verified: clean 3-sentence summary, no reasoning.
Server restarted + HTTP-verified after the `draft.py`/`summarize.py` edits.

## Fact->source trace false-UNVERIFIED fixes (2026-07-21)
Reviewing the same PSA's Review pack, the "Fact -> source trace" showed 3 red ⚠ UNVERIFIED rows
that were NOT real problems (guardrail was CLEAN). Root-caused + fixed all three:
- **Elided quotes** (`summarize.verify_quote`): the model quotes non-contiguous language joined
  by an ellipsis ("A ... B") or with a trailing "...". The verbatim substring check failed on
  these, showing genuine facts (the 79,325 sq ft building; the termination right) as UNVERIFIED.
  Fix: if the whole-quote check fails, split on `...`/`…` and accept iff EVERY segment appears
  verbatim in the source in left-to-right order (the elided middle is what "..." denotes). Each
  segment must still match verbatim, so a paraphrase or a fabricated segment still FAILS (tested).
- **Boilerplate in the trace** (`draft._TRACE_BOILERPLATE`, applied in `draft_8k` + api
  `_recompute_verification`): the model lists the (c) material-relationship statement / the
  exhibit qualifier in `facts_used` using their OWN text as the quote, so they can never verify
  against the contract → a misleading red row. These are required legal assertions, not
  source-grounded facts (already covered by the SEC-requirement checks), so they're dropped from
  the trace.
- **Reverify parity:** `_recompute_verification` now also refreshes the trace (same boilerplate
  drop + ellipsis-aware re-verify), so an edit/supplement can't leave it stale. Verified E2E via
  the reverify path on the stored PSA: 8 rows/3 UNVERIFIED -> 7 rows/0 UNVERIFIED, guardrail clean.
Server restarted + HTTP health-checked after the `draft.py`/`summarize.py`/`api.py` edits.

## Full RR exhibit set downloaded for multi-exhibit testing (2026-07-22)
For the testing/compare phase we replicate real Richtech filings from SEC (the eventual product
lets users upload their own docs). Generalized the EX-99-only `download_rr_supplements.py` into
**`scripts/download_rr_exhibits.py`** — fetches EVERY exhibit type across our reference 8-Ks and
routes by role: agreements/instruments (EX-1.x, EX-4.x, EX-10.x) → `data/RR contracts/`;
opinions/consents/news (EX-5.x, EX-23.x, EX-99.x) → `data/RR supplements/`. Accessions derived
from the `data/RR 8-K/*.pdf` filenames; EDGAR fair-access (declared UA + rate limit); skips
already-present curated files. Downloaded 15 new (skipped 24). Real Richtech exhibit landscape:
EX-1.1 underwriting, EX-4.x securities instruments (warrants), EX-5.1 legal opinion, EX-10.x
contracts, EX-99.1–99.5 press releases (NO 23.1 — those ride with S-1/S-3, not 8-Ks). Richest
compare samples now complete on disk: **2024-09-05 (076143)** SPA+3 warrants+2 press releases;
**2023-11-22 (089609)** underwriting+warrant+5 press releases; **2026-01-30 (009823)** SPA+RRA+2 PR.

## Business-context merge now syncs the per-Item sections (2026-07-21)
Compared our draft of the EBS Rainbow PSA against the **real filed 8-K** (accession
0001213900-26-041153). The only substantive gap was the real filing's forward-looking
business-context sentence ("The Company intends to utilize the Property as a strategic
U.S.-based facility … to support the continuous improvement of the Company's robotics and AI
systems") + the PSLRA Forward-Looking Statements safe-harbor legend it triggers — both by
design supplied by the human via the "Business / strategic context" box, not the model. Our
`_FORWARD_LOOKING_STATEMENTS` is byte-for-byte identical to the real filing's legend.
- **Bug found while testing that flow:** `draft.add_business_context` merged the context into
  the top-level `disclosure` but NOT the matching `_items[]` section. Export and the on-screen
  "Filing content" render from `_items`, so the merged sentence was invisible in the actual
  filing while the FLS legend still appeared → an FLS legend with no forward-looking sentence
  in the body. Fixed: also update the primary (non-cross-ref) `_items` section's opening
  paragraph. Verified: after the fix both top-level and `_items[0]` carry the sentence, in the
  right position (after the asset description, before the price), FLS present.
  - NOTE: pre-fix generations (e.g. the one downloaded as "Draft (13)") stay stale in the DB;
    re-add the business context (or regenerate) to pick up the sync.
- **Remaining real gap vs the filing (candidate next fix):** the real Exhibit index adds the
  parties to the 10.1 description ("… by and between the Company and PSIF EBS Rainbow LLC") and
  a `*` **Item 601(a)(5)** footnote ("Certain annexes, schedules and exhibits have been omitted
  … agrees to furnish supplementally …") because the PSA's Exhibits A–E are omitted. Ours omits
  both. (Cosmetic-only deltas: real signature date = filing date Apr 7 vs our event date Apr 1.)
Server restarted + HTTP-verified after the `draft.py` edit.

## delex fixes + corpus filter shipped — delex fits 2.03/3.02, NOT the 1.01 core (2026-07-16)
Did the Jetson-side work (no RTX needed): fixed delex quality + built the groundability
filter. Both are in the repo (pushed). **RTX handoff (tables + commands): `training/DELEX_V5_FINDINGS.md`.**
- **`training/llamafactory/delex.py` fixed** — three changes, all backfill-consistent:
  1. **input-first numbering** in `process()` (was output-first) — THE confirmed wrong-org
     blocker. Inference (`delex_backfill.delex_source`) numbers input-only, so now a
     placeholder the model copies resolves to the same source value.
  2. **lossless value canon** — `canon_num` unit-expands `$X million`→digits + strips
     format; new `canon_date`→ISO. So `$38.7 million`==`$38,700,000`, `August 20, 2024`==
     `2024-08-20` share ONE placeholder. VALUE-preserving, NOT byte (a within-doc format
     variant backfills to the canonical form). **Rounding stays distinct** (`$38.7M` ≠
     `$38,675,000`) — verified `canon_num` equal ⟺ equal value, so no two different
     material figures ever merge (would be unsafe to backfill). Fuzzy match deliberately
     NOT used.
  3. **tightened ORG regex** — connectors are space/comma/& only (no newline) + every token
     must start with a letter, so a match can't cross a sentence/line boundary or swallow an
     adjacent date (`On\nAugust 20, 2024, Foo Inc.` no longer captured as one ORG).
  - `MAX_INPUT=24000` module const; `delex_backfill.SOURCE_WINDOW` now references it (was a
     hidden 15000 second truncation, tighter than the 24k build cap). `test_delex_backfill.py`
     roundtrip relaxed to value/content-preserving (whitespace-normalized) — PASS; a changed
     digit/letter still fails it. Production path (v2, `mode="hybrid"`) does NOT use delex, so
     unaffected.
- **`training/llamafactory/delex_filter.py`** — keeps only pairs whose OUTPUT placeholders
  are ≥threshold grounded in the input (backfillable), using the same delex+24k window as
  training. Full corpus @0.90: **KEPT 1005/2174 (46%), kept-set mean groundability 99.9%.**
  Output (gitignored): `data/multico_all/train_pairs_delex_filtered.jsonl`.
- **DECISIVE by-Item split — delex is Item-dependent:**
  - transaction Items ground well: **2.03 66/84 (78%), 3.02 61/78 (78%)** — disclosures copy
    the note/SPA's exact figures.
  - **1.01 (the CORE) 17/245 (6%), 5.02 1/128 (0%)** — narrative disclosures paraphrase +
    cite facts outside the exhibit; cannot be delexed.
  - the 1005 kept are **852 news (2.02/7.01/8.01) + only 153 contract-family**, and contract
    is almost all 2.03/3.02. News Items are NOT the drafting product.
- **Recommendation / decision for the user + RTX:**
  - delex only serves **transactional 2.03/3.02** (Richtech financings/notes — genuinely
    useful), NOT the narrative **1.01/5.02** core. For 1.01/5.02 the "zero imagination" bar
    is ALREADY met by `mode="assemble"` (deterministic) / `mode="hybrid"`+guardrail — delex
    adds nothing there.
  - **Cheap path if wanted:** RTX trains v5 on the 1005-pair filtered set (input-first, 24k,
    ~2h DDP — NOT ZeRO-3). Expect it to work for 2.03/3.02+news; re-test on a real Richtech
    3.02 SPA on Jetson. 1.01/5.02 stay on assemble/hybrid.
  - **Or shelve delex** entirely since 1.01 is the priority and assemble/hybrid already
    guarantee no fabrication. Either way, the earlier ZeRO-3 long-context plan is DEAD.

## Later / optional (recommended order: A, scoped-AMBER)
- **A — scale corpus** to ~3,000+ pairs (~300 more small/mid-cap companies; edit
  `COMPANIES` in `training/scrape_all_items.py`) and retrain v3 — ONLY if a future need
  justifies it (v3 data-clean retrain was a negative result; v2 is final for now).
- **Scoped AMBER (guardrail Option B)** — wire the rubric→keyword mapping so omissions of
  MUST-disclose fields surface as AMBER (still non-blocking). See [[guardrail-red-only]].

## Key locations
- **8-K drafting engine**: `lawrag/draft.py` (ITEM_CHECKLISTS, ITEM_RULES w/ materiality
  rubric, _compliance_flags, add_business_context, FLS legend). Facts always via RAG,
  never fine-tuned in.
- **Export (EDGAR-faithful Word/PDF)**: `lawrag/export.py`. **Web**: `web/` +
  `lawrag/api.py`; History/Generate-8-K/business-context/per-Item view all wired.
- **Training package**: `training/` (README.md, DEPLOY_THOR.md, dataset/
  train_pairs.jsonl.gz, prepare_data.py, scrape_all_items.py, build_training_pairs.py,
  llamafactory/ configs, eval_ab_lf.py, adapter-8k-v2/).
- **Corpus (gitignored, on-disk)**: `data/multico_all/` (all-items) and `data/multico/`
  (1.01 pilots). Richtech's own filings: `data/RR 8-K/`, `data/RR contracts/`.
- **Scratchpad scripts are EPHEMERAL** (wiped on session clear); the important ones were
  copied into `training/`.

## Standing rules (do not break)
- **Reply to the user in Chinese** (repeatedly, emphatically requested).
- **Never commit**: anything under `data/`, `storage/`, `README.zh-CN.md` (keep in sync
  locally, English `README.md` only to GitHub), or `Richtech Materials for Potential AI
  Training.docx` (confidential). All are gitignored — keep it that way.
- Facts in any draft must come from the source document (RAG), never model memory.
- Every draft requires lawyer sign-off before filing; this stays an experiment.
- After editing any `lawrag/*.py`, restart the web server AND verify via a real HTTP
  request (Python has no hot-reload; a stale/hung server has bitten us before).
- The web server is a manual `nohup` process — not auto-restart / not reboot-safe
  (systemd service still TODO if it needs to be always-up).

## Product direction (agreed)
Commercial, multi-company. **Shared base + one LoRA adapter per filing type** (8-K done;
S-8 easy next; 10-K is a much bigger separate project). **Per-customer style = data
(their filings via RAG + auto materiality rubric), NOT per-customer fine-tuning.**
Single-tenant-per-deployment for confidentiality. GPU (RTX 6000) also becomes the
self-hosted inference server for real customers (Jetson won't scale for production).
