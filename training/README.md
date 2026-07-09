# 8-K LoRA adapter — training package

Self-contained package to fine-tune the **8-K adapter** for `Qwen3.6-35B-A3B` on an
**RTX 6000** (48 GB Ada or 96 GB Blackwell) or an **RTX 5090** (32 GB, QLoRA with a
smaller sequence length). Clone the repo on that machine and run the steps below.

## What this is (and isn't)

- One **LoRA adapter** on a shared frozen base — the first block of a
  "base + per-filing-type adapters" architecture (8-K now; S-8 / 10-K later each get
  their own adapter on the same base).
- It learns 8-K **style, structure, and materiality selectivity** — *not facts*. At
  inference the real names/dates/amounts still come from the source contract (RAG).
  Do not treat the adapter as a source of facts.
- The point of this first run is a **A/B test**: does the adapter beat the un-tuned
  base? Decide whether to scale up (more data) based on `eval_ab.py` + a human read.

## Data

`dataset/train_pairs.jsonl.gz` (8 MB, ~2,174 pairs) is included — built from **public
SEC EDGAR filings** of ~90 companies (`{instruction, input=source doc, output=real
Item disclosure, meta}`). No confidential data. To regenerate/extend it, see
"Regenerating the corpus" below.

## Setup

```bash
cd training
python -m venv .venv && source .venv/bin/activate

# 1) Install torch matching the card's CUDA FIRST:
#    Blackwell (5090 / PRO 6000 Blackwell):
pip install torch --index-url https://download.pytorch.org/whl/cu128
#    RTX 6000 Ada (CUDA 12.1/12.4): the default wheel is fine:
# pip install torch
pip install -r requirements.txt

# 2) Point at the TRAINABLE bf16 base (NOT the NVFP4 inference build).
#    Verify the exact HF repo id, or use a local path to downloaded weights.
export BASE_MODEL=Qwen/Qwen3.6-35B-A3B        # <-- confirm this id
huggingface-cli download "$BASE_MODEL"        # ~70 GB, one time
```

## Train

```bash
python prepare_data.py            # -> dataset/train.jsonl + val.jsonl (split by company)
python train_lora.py              # QLoRA; saves ./adapter-8k (a few hundred MB)
# 5090 / tight VRAM: python train_lora.py --max-seq-len 6144
```

First-run tips:
- **OOM?** lower `--max-seq-len` (8192 → 6144 → 4096), keep batch size 1.
- If `target_modules="all-linear"` errors on the MoE, edit `train_lora.py` to the
  explicit list noted there.
- Watch the loss for the first ~20 steps; it should decrease steadily.

## Evaluate (the A/B)

```bash
python eval_ab.py --n 40          # base (no FT) vs base + adapter, held-out companies
# prints ROUGE-L + number-recall for each, and writes eval_samples.txt (read it!)
```

Interpretation: the adapter is worth scaling if it clearly improves the metrics AND
the side-by-side samples read closer to the real filings (tighter, right terms
disclosed, correct closing qualifier). If it's a wash, RAG + the materiality rubric
is already enough — don't invest further.

## Regenerating / extending the corpus (optional)

```bash
export CORPUS_DIR=./corpus
python scrape_all_items.py        # pulls 8-Ks + exhibits from EDGAR (edit COMPANIES list)
python build_training_pairs.py    # -> corpus/train_pairs.jsonl (+ samples)
gzip -c corpus/train_pairs.jsonl > dataset/train_pairs.jsonl.gz
```

Add more companies by editing the `COMPANIES` list in `scrape_all_items.py` (weight
toward small/mid-cap active filers — the customer profile). Yield is ~5–6 clean
contract-family pairs per company; ~300 more companies ≈ ~3,000 contract pairs.

## Using the adapter for inference

Load the same base, attach the adapter, and keep facts coming from the source
contract via the existing RAG pipeline:

```python
from peft import PeftModel
model = PeftModel.from_pretrained(base_model, "training/adapter-8k")
```

The adapter is ~hundreds of MB — small enough to version and ship per deployment.
