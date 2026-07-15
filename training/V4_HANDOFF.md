# v4 (delexicalized adapter) — handoff to the Jetson/app side

**Built & validated on the RTX box, 2026-07-15.** Decision needed from the Jetson side
(build the backfill, decide whether v4 replaces v2 in production).

## What v4 is (one line)

Training data has deal-specific facts replaced with **typed indexed placeholders**, so the
adapter learns 8-K **structure / tone / materiality only** — never real values, so it
**structurally cannot fabricate numbers**. Facts are backfilled from the source on Jetson.

## Result (validated on held-out val)

- **12/12 val outputs: ZERO financial-fact fabrication** — all $ amounts, share counts,
  percentages, dates, company/person names come out as placeholders.
- **Structure + legal boilerplate preserved**: `Section 18 of the Securities Exchange Act
  of 1934`, `shall not be deemed "filed"`, `Exhibit 99.1`, `Form 8-K`, `Item X.XX` all intact.
- Contrast: v2 emits real (often wrong) numbers → fabrication; v4 emits placeholders →
  fabrication removed at the source. (Jetson already confirmed the RED guardrail catches
  v2's fabrication in production; v4 makes that the exception, not the rule.)

## Placeholder schema (the backfill must reverse this)

| Placeholder | Covers | How detected |
|---|---|---|
| `[AMOUNT_n]` | `$` amounts | regex |
| `[NUM_n]` | comma-grouped numbers / share counts | regex |
| `[PCT_n]` | percentages | regex |
| `[DATE_n]` | `Month DD, YYYY` / ISO / `MM/DD/YYYY` | regex |
| `[ORG_n]` | company names (corporate-suffix Inc./LLC/Corp… + bare repeats) | regex |
| `[PERSON_n]` | person names | spaCy PERSON + boilerplate stoplist |

**Consistency rule (key):** the same real entity gets the **same placeholder in input and
output**; numbered per type, output processed before input, index by first appearance.
**NOT masked (kept as learnable structure):** legal/regulatory boilerplate — Securities
Exchange Act, SEC, Form 8-K, Item/Exhibit/Section numbers, years like 1934.

## Backfill mechanism (simpler than it looks — direct substitution)

Because placeholders are consistent input↔output, backfill is mostly **one dictionary
substitution**, no separate alignment engine:

1. Run the **same delex on the incoming source doc** → get a `placeholder → real value`
   map (e.g. `[AMOUNT_1] → $5,000,000`).
2. Feed the delexed source to v4 → v4 emits a delexed skeleton (same placeholders).
3. **Substitute** the skeleton's placeholders back to real values using the map from (1).
4. **Guardrail verifies**: if the model emitted a placeholder index absent from the source
   map, or an off-by-one index, RED-flag it — this composes with the existing RED logic.

The only correctness point is whether the model faithfully copies placeholder indices
(it overwhelmingly does; the guardrail backstops the rest). Reuse `llamafactory/delex.py`
(this repo) for step 1 so masking is identical to training.

## Artifacts (on RTX, pull when ready)

- Adapter: `/mnt/raid/law_rag_8k/output/adapter-8k-v4` (~83 MB bf16 LoRA; same base
  `/mnt/raid/AAA.3A/model`, same safe LoRA targets as v2)
- Delex data: `/mnt/raid/law_rag_8k/data_v4/` (train 1922 / val 252)
- Delex script: `training/llamafactory/delex.py` (this commit) — **the backfill needs its
  inverse; keep masking identical to it**
- Train config: `config/qwen36_8k_lora_v4.yaml`
- NOT yet quantized — quantize to NVFP4 only after e2e backfill validation.

## Residual limitations (v5, non-blocking)

Product names (iPhone 14 Pro), locations (Zhengzhou), and bare years (2022) are not
masked — low risk (usually copied from source, not the catastrophic $/share fabrication).
Add to the delex if you want them masked too.

## When v4 is quantized to NVFP4 later — remember the processor_config.json gotcha

v4 is the same multimodal base as v2, so the NVFP4 export will again omit
`processor_config.json` and vLLM will crash-loop without it. RTX will copy it into the v4
NVFP4 dir before handoff (same fix you applied for v2).

## Decisions for Jetson

1. Build the backfill (placeholder → verified source value + guardrail verify); validate
   e2e (v4 skeleton + backfill + guardrail).
2. If it validates, tell RTX to quantize v4 → NVFP4 (same path as v2, + processor_config.json).
3. Does v4 replace v2 in production? (v2: fabricates, guardrail catches; v4: structurally
   can't fabricate $/shares, needs backfill — cleaner.)
