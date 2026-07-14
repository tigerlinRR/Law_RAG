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
