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

## ✅ The NVFP4 model is already built (on the RTX 6000)

Serving stack = **vLLM / NVFP4** (not GGUF): `lawrag/llm.py` depends on vLLM's
`guided_json` for `draft_8k`'s structured output, and Thor already runs vLLM+NVFP4.

The 8-K adapter (bf16 LoRA) has been **merged into the base and quantized to NVFP4**
already, on the RTX box, and validated (coherent post-quant output, correct source
figures). You do **not** need to merge or quantize on Thor.

**Artifact (on RTX, transfer via rsync):** `/mnt/raid/law_rag_8k/output/adapter-8k-v2-nvfp4/`
(~21 GB, 3 safetensors shards + `hf_quant_config.json` `quant_algo: NVFP4` +
`tight_template.jinja`). Merging added zero inference cost (same A3B active experts) —
on Thor it runs at your current 35B's speed, plus the 8-K skill.

### Step 1 — copy the NVFP4 model to Thor

```bash
rsync -avP <rtx-user>@<rtx-ip>:/mnt/raid/law_rag_8k/output/adapter-8k-v2-nvfp4/ ./qwen36-8k-nvfp4/
```

### Step 2 — serve on Thor (exactly like your current base, swap the path)

```bash
vllm serve ./qwen36-8k-nvfp4 \
  --served-model-name qwen3.6-8k \
  --chat-template ./qwen36-8k-nvfp4/tight_template.jinja \
  --host 0.0.0.0 --port 8012
```

- vLLM auto-detects NVFP4 from `hf_quant_config.json` (modelopt format).
- `--chat-template tight_template.jinja` forces thinking OFF (empty `<think></think>`)
  so the model emits the tight filing directly instead of a verbose reasoning preamble.
  (Alternatively pass `chat_template_kwargs={"enable_thinking": false}` per request.)
- `guided_json` works → `draft_8k` structured output is unchanged.

### Re-quantizing later (if you ever need to)

modelopt 0.45 + `transformers>=5.x` (5.x is required to load the `qwen3_5_moe` arch;
modelopt 0.43 pins transformers<5 and cannot). Do **not** feed the on-disk
`adapter-8k-v2-merged/` to `from_pretrained` — LLaMA-Factory's export gave it corrupt
triple-nested `language_model.language_model.language_model` keys (GGUF-convert and the
LF loader tolerate it; vanilla transformers reinitializes all weights → garbage). Load
base + adapter via LLaMA-Factory `ChatModel` then `merge_and_unload`, then quantize.
modelopt auto-excludes the sensitive modules (linear_attn conv1d/in_proj_a/b, mlp gates,
lm_head, embed_tokens), which is why the quantized model stays coherent.

**GGUF fallback:** `qwen36-8k-Q4_K_M.gguf` (~20 GB) exists as a portable llama.cpp
option, but loses vLLM's `guided_json` — prefer NVFP4 above.

## Wiring into Law_RAG (8-K generation only — no RAG needed)

For the pure "generate an 8-K from a source doc" task you do **not** need the DB /
embedding / rerank services — it's a doc-in → disclosure-out transform. Minimal setup:

1. Serve the NVFP4 model (Step 2 above).
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
