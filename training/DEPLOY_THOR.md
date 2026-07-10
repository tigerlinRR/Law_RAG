# 8-K adapter — deploy on Jetson AGX Thor

The trained **8-K drafting LoRA adapter** for `Qwen3.6-35B-A3B`. It teaches the model
to draft SEC Form 8-K Item disclosures in real-filing style/structure from a source
document (contract / press release / news). Facts come from the source doc you pass
in — the adapter learns *style, structure, and materiality selectivity*, not facts.

## A/B result (held-out companies, greedy, LLaMA-Factory inference)

| metric | base (no FT) | + 8-K adapter |
|---|---:|---:|
| ROUGE-L (vs real filing) | 0.246 | **0.464** (~+89%) |
| number-recall (material figures) | 0.577 | **0.675** |
| avg output length | 2430 char | **1098 char** (tighter, like real filings) |

The base rambles / emits `<think>` reasoning; the adapter goes straight to a tight,
correctly-formatted disclosure. Clear, reproducible win.

## What's here

```
training/adapter-8k-v2/          # the adapter — THIS is the deliverable (~83 MB)
  adapter_config.json            #   base = Qwen/Qwen3.6-35B-A3B, r32/α64,
  adapter_model.safetensors      #   targets: q,k,v,o,gate,up,down_proj (safe set)
  tokenizer*.json, chat_template.jinja
training/llamafactory/           # reproducibility
  qwen36_8k_lora_v2.yaml         #   LLaMA-Factory train config
  merge_8k_v2.yaml               #   merge config
  convert_to_lf.py               #   EDGAR pairs -> LLaMA-Factory alpaca format
training/eval_ab_lf.py           # the CORRECT A/B eval (uses LLaMA-Factory ChatModel)
```

## ⚠️ Key deployment fact

The adapter is a **bf16 LoRA**. Thor serves the base as **NVFP4** (4-bit). You cannot
attach a bf16 LoRA to an NVFP4 model directly — you must **merge, then re-quantize**:

```
bf16 base + bf16 adapter  --merge-->  bf16 merged model  --YOUR NVFP4 quant-->  serve
```

Do the merge+quant on a machine with the bf16 base + a GPU (the RTX 6000 box), not on
Thor. Merging adds **zero** inference cost — same params, same A3B active experts, so
on Thor it runs at exactly the speed of your current 35B, just with the 8-K skill.

### Step 1 — merge (on the RTX 6000 box, LLaMA-Factory)

```bash
llamafactory-cli export training/llamafactory/merge_8k_v2.yaml
# -> a full bf16 merged model (edit paths in the yaml first)
```

### Step 2 — quantize to NVFP4

Run the merged bf16 model through **the same NVFP4 pipeline you used to build the
base's inference build**. (That toolchain is yours — it's not in this repo.)

### Step 3 — serve on Thor

Point vLLM at the NVFP4 merged model, exactly like your current base:

```bash
# same vLLM invocation you already use, just swap the model path
vllm serve <nvfp4-merged-8k>  --served-model-name qwen3.6-8k  --port 8012
```

## Wiring into Law_RAG (8-K generation only — no RAG needed)

For the pure "generate an 8-K from a source doc" task you do **not** need the DB /
embedding / rerank services — it's a doc-in → disclosure-out transform. Minimal setup:

1. Serve the merged-8k model (Step 3).
2. Point `LLM_MODEL` / `LLM_BASE_URL` in `.env` at it.
3. Use the same system prompt the adapter was trained with:

   > You are a securities lawyer drafting U.S. SEC Form 8-K Item disclosures. Write in
   > the concise, neutral style of a real filing, disclosing only material terms and
   > using only facts present in the provided source document.

   User turn: `<instruction>\n\n=== SOURCE DOCUMENT ===\n<source text>`

RAG (Postgres+pgvector, embed, rerank) becomes worthwhile only for the **later**
version — precedent-consistent drafting (retrieve the company's past same-Item 8-Ks),
cross-references to prior filings, and the broader find-documents / due-diligence
features. Add it back as an enhancement layer then.

## Reproduce / extend the training

See `training/README.md`. Data prep: `convert_to_lf.py` turns the repo's 2,174 EDGAR
pairs into LLaMA-Factory alpaca format (split by company). Train with
`qwen36_8k_lora_v2.yaml` (bf16 LoRA, 3-GPU DDP). To improve further, scale the corpus
(~300 more companies ≈ ~3,000 pairs) per `training/README.md`, then retrain.
```
