#!/usr/bin/env python
"""QLoRA fine-tune the 8-K adapter for Qwen3.6-35B-A3B on the prepared pairs.

Runs on a single RTX 6000 (48GB Ada or 96GB Blackwell) — or a 5090 (32GB) with a
smaller --max-seq-len. Loads the base in 4-bit (QLoRA), trains a LoRA adapter, and
saves ONLY the adapter (a few hundred MB) to ./adapter-8k.

The base stays frozen and facts are NOT baked in — this adapter learns 8-K style,
structure and materiality selectivity; at inference the real facts still come from
the source contract (RAG). Keep that separation.

  BASE_MODEL  env var — the bf16 base checkpoint. MUST be the trainable base, NOT
              the NVFP4 (4-bit, inference-only) build. Set it explicitly, e.g.:
              export BASE_MODEL=Qwen/Qwen3.6-35B-A3B      # verify the exact repo id
              (or a local path to downloaded bf16 weights)

Usage:
  export BASE_MODEL=<bf16 base repo id or local path>
  python prepare_data.py
  python train_lora.py                     # sensible defaults
  python train_lora.py --max-seq-len 6144  # lower if you OOM (esp. on a 5090)
"""
import argparse
import json
import os
from pathlib import Path

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                          TrainingArguments)
from trl import SFTTrainer, DataCollatorForCompletionOnlyLM

HERE = Path(__file__).resolve().parent
SYSTEM = ("You are a securities lawyer drafting U.S. SEC Form 8-K Item disclosures. "
          "Write in the concise, neutral style of a real filing, disclosing only "
          "material terms and using only facts present in the provided source document.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-model", default=os.getenv("BASE_MODEL", ""))
    ap.add_argument("--train-file", default=str(HERE / "dataset" / "train.jsonl"))
    ap.add_argument("--out", default=str(HERE / "adapter-8k"))
    ap.add_argument("--max-seq-len", type=int, default=8192)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--lora-r", type=int, default=32)
    ap.add_argument("--lora-alpha", type=int, default=64)
    args = ap.parse_args()

    if not args.base_model:
        raise SystemExit(
            "Set BASE_MODEL to the bf16 trainable base (NOT the NVFP4 build).\n"
            "  export BASE_MODEL=Qwen/Qwen3.6-35B-A3B   # verify the exact HF repo id")

    tok = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    bnb = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model, quantization_config=bnb, torch_dtype=torch.bfloat16,
        device_map="auto", trust_remote_code=True)
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        # "all-linear" is robust across the MoE's expert/attention naming; it
        # attaches LoRA to every linear layer (excluding the router where peft
        # can auto-skip). If it errors on your build, replace with an explicit
        # list: ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"].
        target_modules="all-linear")
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    def fmt(ex):
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": ex["instruction"] + "\n\n=== SOURCE DOCUMENT ===\n" + ex["input"]},
                {"role": "assistant", "content": ex["output"]}]
        return tok.apply_chat_template(msgs, tokenize=False)

    ds = load_dataset("json", data_files=args.train_file, split="train")
    # completion-only: mask everything before the assistant turn so we train only
    # on producing the disclosure, not on echoing the (long) contract prompt.
    resp_tmpl = "<|im_start|>assistant\n"
    collator = DataCollatorForCompletionOnlyLM(response_template=resp_tmpl, tokenizer=tok)

    targs = TrainingArguments(
        output_dir=str(HERE / "_train_out"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        gradient_checkpointing=True,
        learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
        bf16=True, logging_steps=5, save_strategy="epoch", save_total_limit=1,
        optim="paged_adamw_8bit", report_to="none")

    trainer = SFTTrainer(
        model=model, args=targs, train_dataset=ds,
        formatting_func=fmt, data_collator=collator,
        max_seq_length=args.max_seq_len, tokenizer=tok, packing=False)
    trainer.train()

    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"\nSaved 8-K LoRA adapter -> {args.out}")
    print("Load at inference with: PeftModel.from_pretrained(base, '{}')".format(args.out))


if __name__ == "__main__":
    main()
