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

## Immediate next steps (recommended order: C → B, then A)
- **C — human/legal quality check** of `training/eval_samples_lf.txt` (generated on the
  RTX box, not in repo — ask user to paste a few). Verify: no invented facts not in the
  source (compliance red line); tighter output didn't drop required elements. Can run
  `draft._compliance_flags` over adapter outputs to check (a)-(d) + exhibit qualifier.
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
