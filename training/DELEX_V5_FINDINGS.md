# Delex v5 — findings, fixes, and the decision (Jetson → RTX handoff)

_2026-07-16. Read this after `git pull`. Everything below was measured on Jetson against
the real corpus. TL;DR at top; commands for RTX at the bottom._

---

## TL;DR

1. **Your hypothesis was tested and is false.** The residual delex misalignment is **NOT**
   caused by the 24k source truncation. Widening the training window 15k → full 120k buys
   only **+5 points** of alignment (34.9% → 39.9%). Median source doc is 17.5k, so the full
   text is already in view — the missing facts are simply **not in the paired exhibit**.
2. **=> Do NOT build the full-text corpus + ZeRO-3 long-context run.** It spends the big GPU
   budget on the smallest lever. The ZeRO-3 smoke test is fine as an infra capability check;
   just don't train the long-context model on that premise.
3. **The real fix was cheap and is done on Jetson** (pushed): input-first numbering (your
   wrong-org blocker), lossless value canon, tighter ORG regex, and a **groundability filter**
   that keeps only pairs the model can actually learn to copy.
4. **New, decisive finding: delex is Item-dependent.** It grounds well for **transaction
   Items (2.03 78%, 3.02 78%)** but collapses on the **narrative core (1.01 6%, 5.02 0%)** —
   those disclosures paraphrase and cite facts outside the exhibit, so no window/canon fixes
   them.
5. **Decision for you + Tiger (below):** train a cheap, narrow v5 for 2.03/3.02, **or** shelve
   delex (1.01's "zero imagination" is already met by `assemble`/`hybrid`+guardrail).

---

## 1. What was measured

Raw full-length EDGAR docs are on Jetson (`data/multico_all/`, max 399,888 chars); the 24k
cap was only `build_training_pairs.py:MAX_INPUT_CHARS` at packaging. Rebuilt full-length pairs
and measured **placeholder alignment** = fraction of OUTPUT placeholders whose canonical
`(type, value)` also appears in the INPUT window (= a real backfill anchor). Micro over a
stratified sample.

### 1a. Window is not the bottleneck

| Input window | Overall alignment | Item 1.01 |
|---|---|---|
| 15,000 (old) | 34.9% | 27.8% |
| 24,000 | 36.7% | — |
| 48,000 | 38.6% | — |
| **120,000 (full)** | **39.9%** | **36.3%** |

Full text over 15k = **+5 pts**. That is the entire prize for the long-context project.

### 1b. Where the residual actually is (backfill-SAFE, lossless canon, no fuzzy)

| Type | raw canon | lossless canon |
|---|---|---|
| DATE | 50.1% | 52.8% |
| AMOUNT | 32.0% | **42.6%** |
| ORG | 30.3% | 37.0% |
| NUM | 24.2% | 24.2% |
| PERSON | 22.1% | 22.1% |
| **Overall** | **36.3%** | **40.6%** |

Value-aware canon (`$38.7 million` == `$38,700,000`, dates → ISO) is a cheap +4, roughly
equal to the whole window gain. The rest of the gap is **structural**: ORG/PERSON residual is
largely names absent from the paired exhibit (director-bio past employers, officer names), and
AMOUNTs that the disclosure rounds/paraphrases (`$38.7M` vs a source `$38,675,000` — correctly
kept distinct; we never fuzzy-merge distinct material figures).

**Conclusion: the delex-copy ceiling is ~40–55% because real 8-K disclosures transform facts
and pull from outside the source. No training window fixes that.**

---

## 2. What was fixed on Jetson (pushed — `git pull` to get it)

`training/llamafactory/delex.py`:
- **Input-first numbering** in `process()` (was output-first). This is **your confirmed
  wrong-org blocker.** Inference (`lawrag/delex_backfill.delex_source`) numbers input-only, so
  a placeholder the model copies now resolves to the same source value.
- **Lossless value canon** — `canon_num` unit-expands (`$X million` → digits) and strips
  format; new `canon_date` → ISO. Format variants share one placeholder; **rounding stays
  distinct** (verified: `canon_num` equal ⟺ equal value, so two different material figures
  never merge). Fuzzy matching deliberately not used (unsafe to backfill).
- **Tighter ORG regex** — connectors are space/comma/& only (no newline), every token starts
  with a letter → a match can't cross a sentence/line boundary or swallow an adjacent date.
- `MAX_INPUT = 24000` module constant; `delex_backfill.SOURCE_WINDOW` now references it (it was
  a hidden **15000** second truncation, tighter than the 24k build cap — training and inference
  now use the same window). `DELEX_SRC` / `DELEX_OUT` are env-overridable.
- `tests/test_delex_backfill.py` roundtrip relaxed to value/content-preserving; PASS (a changed
  digit/letter still fails it).

`training/llamafactory/delex_filter.py` (new): keeps only pairs whose OUTPUT placeholders are
≥ threshold grounded in the input, using the same delex + 24k window as training. This makes
supervision consistent — **"only emit placeholders you can copy"** — which directly kills the
ungroundable-placeholder → wrong-org failure.

---

## 3. Filter yield (full corpus, threshold 0.90, 24k window)

**KEPT 1005 / 2174 (46%), kept-set mean groundability 99.9%.**
Shipped: `training/dataset/train_pairs_delex_filtered.jsonl.gz` (1005 pairs, ready to train).

| Item | Family | Kept / total | Delex-viable? |
|---|---|---|---|
| 2.03 (debt) | transaction | 66 / 84 (78%) | ✅ |
| 3.02 (equity) | transaction | 61 / 78 (78%) | ✅ |
| 2.02 (earnings) | news | 407 / 674 (60%) | ✅ (not the drafting product) |
| 8.01 (other) | news | 236 / 467 (50%) | ✅ (not the drafting product) |
| 7.01 (Reg FD) | news | 209 / 481 (43%) | ✅ (not the drafting product) |
| **1.01 (CORE)** | narrative | **17 / 245 (6%)** | ❌ |
| **5.02** | narrative | **1 / 128 (0%)** | ❌ |

The 1005 kept are **852 news + only 153 contract-family**, and contract is almost entirely
2.03/3.02. delex fits transactional Items; it cannot serve the narrative 1.01/5.02 core.

---

## 4. Decision

For **1.01 / 5.02**, the "zero imagination" bar is already met without delex:
`draft_8k(mode="assemble")` (deterministic assembly) and `mode="hybrid"` (LLM prose +
guardrail blanks ungrounded figures). delex adds nothing there.

- **Option A (recommended): train a cheap, narrow v5.** RTX trains on the 1005 filtered pairs
  (input-first, 24k, ~2h DDP — **NOT** ZeRO-3). Expect it to work for 2.03/3.02 + news;
  re-test on a real Richtech 3.02 SPA on Jetson. 1.01/5.02 stay on assemble/hybrid. Low cost,
  validates the whole delex pipeline (esp. that input-first numbering fixes wrong-org).
- **Option B: shelve delex.** 1.01 is the priority and assemble/hybrid already guarantee no
  fabrication; 2.03/3.02 delex can wait until those Items are a business priority.

Either way: **the ZeRO-3 long-context plan is dead.**

---

## 5. Commands for RTX (Option A)

```bash
git pull                                   # gets fixed delex.py + the filtered dataset

# 1) delex the filtered set with the FIXED logic (input-first, lossless canon, ORG fix).
#    Env vars point delex.py at the v5 dataset + output dir (adjust to your box):
cd training/llamafactory
DELEX_SRC=../dataset/train_pairs_delex_filtered.jsonl.gz \
DELEX_OUT=/mnt/raid/law_rag_8k/data_v5 \
  python delex.py
#    -> writes lawrag_8k_v4_{train,val}.json + dataset_info.json into data_v5
#       (internal names still say v4; rename or point your config at them)

# 2) train the LoRA exactly like v4 (same safe target set, DDP, ~2h) — NOT ZeRO-3.
#    Reuse your v4 LLaMA-Factory config, just swap the dataset dir to data_v5.

# 3) merge bf16 base+adapter -> NVFP4 (your existing toolchain) -> ship to Jetson as before.
```

**Sanity check before training:** the filter guarantees ≥90% of each kept pair's output
placeholders are input-grounded — so after `delex.py`, the train JSON's outputs should have
almost no placeholder index that doesn't also appear in its input. If you see many orphan
indices, the delex version is mismatched (confirm `git pull` took).

**Do NOT** rebuild a full-text corpus or set up ZeRO-3 for this — it is not the bottleneck
(section 1).
