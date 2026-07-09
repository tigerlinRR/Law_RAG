#!/usr/bin/env python
"""A/B eval: does the 8-K adapter beat the un-tuned base on held-out companies?

For each val pair, generate a disclosure from the same base model TWICE — once with
the LoRA adapter OFF (the "no fine-tune" baseline) and once with it ON — then score
both against the real filed disclosure. Same base, same prompt, adapter toggled: a
clean isolation of the adapter's effect.

Metrics (lightweight, deterministic):
  - ROUGE-L F1 vs the real disclosure (structural/lexical overlap)
  - number-recall: fraction of $ amounts / key figures in the real disclosure that
    also appear in the draft (a proxy for capturing the material terms)

This is a directional signal, not a legal-quality verdict — pair it with a human
read of eval_samples.txt before deciding to scale up.

Usage:
  export BASE_MODEL=<same bf16 base used for training>
  python eval_ab.py --adapter ./adapter-8k --n 40
"""
import argparse
import json
import os
import re
from pathlib import Path

import torch
from peft import PeftModel
from rouge_score import rouge_scorer
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

HERE = Path(__file__).resolve().parent
SYSTEM = ("You are a securities lawyer drafting U.S. SEC Form 8-K Item disclosures. "
          "Write in the concise, neutral style of a real filing, disclosing only "
          "material terms and using only facts present in the provided source document.")
_NUM = re.compile(r"\$[\d,]+(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?%?\b")


def number_recall(ref: str, hyp: str) -> float:
    refs = set(_NUM.findall(ref))
    if not refs:
        return 1.0
    hyps = set(_NUM.findall(hyp))
    return len(refs & hyps) / len(refs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-model", default=os.getenv("BASE_MODEL", ""))
    ap.add_argument("--adapter", default=str(HERE / "adapter-8k"))
    ap.add_argument("--val-file", default=str(HERE / "dataset" / "val.jsonl"))
    ap.add_argument("--n", type=int, default=40, help="how many val pairs to score")
    ap.add_argument("--max-new-tokens", type=int, default=1200)
    args = ap.parse_args()
    if not args.base_model:
        raise SystemExit("Set BASE_MODEL to the same bf16 base used for training.")

    tok = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    base = AutoModelForCausalLM.from_pretrained(
        args.base_model, quantization_config=bnb, torch_dtype=torch.bfloat16,
        device_map="auto", trust_remote_code=True)
    model = PeftModel.from_pretrained(base, args.adapter)  # adapter attached, toggle below
    model.eval()

    rows = [json.loads(l) for l in Path(args.val_file).read_text().splitlines() if l.strip()]
    rows = rows[:args.n]
    scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)

    def gen(ex, adapter_on):
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": ex["instruction"] + "\n\n=== SOURCE DOCUMENT ===\n" + ex["input"]}]
        prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        ids = tok(prompt, return_tensors="pt", truncation=True, max_length=8192).to(model.device)
        ctx = model.disable_adapter() if not adapter_on else _null()
        with torch.no_grad(), ctx:
            out = model.generate(**ids, max_new_tokens=args.max_new_tokens,
                                 do_sample=False, pad_token_id=tok.eos_token_id)
        return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True).strip()

    agg = {"base": {"rouge": [], "num": []}, "adapter": {"rouge": [], "num": []}}
    samples = []
    for i, ex in enumerate(rows):
        ref = ex["output"]
        b = gen(ex, adapter_on=False)
        a = gen(ex, adapter_on=True)
        agg["base"]["rouge"].append(scorer.score(ref, b)["rougeL"].fmeasure)
        agg["base"]["num"].append(number_recall(ref, b))
        agg["adapter"]["rouge"].append(scorer.score(ref, a)["rougeL"].fmeasure)
        agg["adapter"]["num"].append(number_recall(ref, a))
        if i < 5:
            samples.append((ex, b, a))
        print(f"[{i+1}/{len(rows)}] {ex['ticker']} {ex['item']}", flush=True)

    def avg(x):
        return sum(x) / max(len(x), 1)
    print("\n===== A/B RESULT (held-out companies) =====")
    print(f"{'metric':14} {'base (no FT)':>14} {'+ 8-K adapter':>14}")
    print(f"{'ROUGE-L':14} {avg(agg['base']['rouge']):>14.3f} {avg(agg['adapter']['rouge']):>14.3f}")
    print(f"{'number-recall':14} {avg(agg['base']['num']):>14.3f} {avg(agg['adapter']['num']):>14.3f}")

    with open(HERE / "eval_samples.txt", "w") as fh:
        for ex, b, a in samples:
            fh.write(f"===== {ex['ticker']} Item {ex['item']} ({ex.get('deal_type')}) =====\n")
            fh.write(f"--- REAL DISCLOSURE ---\n{ex['output']}\n\n")
            fh.write(f"--- BASE (no fine-tune) ---\n{b}\n\n")
            fh.write(f"--- + 8-K ADAPTER ---\n{a}\n\n\n")
    print(f"\nWrote 5 side-by-side samples -> {HERE/'eval_samples.txt'} (read these too!)")


class _null:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    main()
