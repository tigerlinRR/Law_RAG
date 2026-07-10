# Law_RAG — Progress / Restart Context

Living status doc so a fresh chat can resume fast. Pairs with:
- **Auto-memory** `~/.claude/projects/-home-jetson-Desktop-Law-RAG/memory/` (read
  `MEMORY.md` index; the dense history is in `law-rag-project-plan.md`).
- **`CLAUDE.md`** (points here) and the repo `README.md` (product/architecture docs).

_Last updated: 2026-07-10._

## What this is
Fully-local, private RAG + drafting system for Richtech's legal counsel, on a Jetson
AGX Thor. Two things live here: (1) the **8-K drafting** tool (contract → SEC Form 8-K
Item disclosure, RAG-grounded), and (2) an experimental **fine-tuned 8-K style
adapter**. Goal is to commercialize as a per-company product (shared base + per-filing
adapter + per-customer style via RAG/rubric).

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

## Immediate next steps (recommended order: B, then A)
- **C — DONE (2026-07-10)** — see the "C — human/legal quality check DONE" section
  above. Bottom line: adapter is a style layer, not a fact source; deploy accordingly.
- **B — deploy v2 on Thor**: merge (bf16 base + adapter → bf16 merged) on the RTX box
  via `training/llamafactory/merge_8k_v2.yaml`, then re-quantize to NVFP4 with the
  user's own toolchain, serve via vLLM (swap model path), point `.env` `LLM_MODEL`/
  `LLM_BASE_URL` at it. Merge = zero inference cost. See `training/DEPLOY_THOR.md`.
  Pure 8-K generation is doc-in→out; does NOT need the DB/embed/rerank services.
- **A — scale corpus** to ~3,000+ pairs (~300 more small/mid-cap companies; edit
  `COMPANIES` in `training/scrape_all_items.py`) and retrain v3, if C/B justify it.

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
