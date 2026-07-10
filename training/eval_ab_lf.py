#!/usr/bin/env python
"""A/B eval done RIGHT — via LLaMA-Factory's ChatModel for BOTH sides, so base and
adapter use the exact qwen3_5 template the adapter was trained with (a tokenizer-
template mismatch is what made the earlier merged-model eval emit gibberish).

  base    = /mnt/raid/AAA.3A/model                     (no adapter)
  adapter = same base + adapter-8k-v2 (LoRA attached)

Sequential load (base first, unload, then adapter) to fit one 96GB card.
Greedy decoding, same settings both sides. Metrics: ROUGE-L F1 + number-recall.

Usage: CUDA_VISIBLE_DEVICES=0 python eval_ab_lf.py --n 40
"""
import argparse
import gc
import json
import re
from pathlib import Path

import torch
from rouge_score import rouge_scorer
from llamafactory.chat import ChatModel

HERE = Path(__file__).resolve().parent
BASE = "/mnt/raid/AAA.3A/model"
ADAPTER = "/mnt/raid/law_rag_8k/output/adapter-8k-v2"
SYSTEM = ("You are a securities lawyer drafting U.S. SEC Form 8-K Item disclosures. "
          "Write in the concise, neutral style of a real filing, disclosing only "
          "material terms and using only facts present in the provided source document.")
_NUM = re.compile(r"\$[\d,]+(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?%?\b")


def number_recall(ref, hyp):
    refs = set(_NUM.findall(ref))
    if not refs:
        return 1.0
    return len(refs & set(_NUM.findall(hyp))) / len(refs)


def gen_all(model, rows, max_new_tokens):
    outs = []
    for i, ex in enumerate(rows):
        user = ex["instruction"] + "\n\n=== SOURCE DOCUMENT ===\n" + ex["input"][:12000]
        resp = model.chat([{"role": "user", "content": user}], system=SYSTEM,
                          do_sample=False, max_new_tokens=max_new_tokens)
        # strip the empty <think>...</think> block the qwen3_5 template emits
        text = resp[0].response_text
        text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
        outs.append(text)
        print(f"[{i+1}/{len(rows)}] {ex['ticker']} {ex['item']}", flush=True)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    args = ap.parse_args()

    rows = [json.loads(l) for l in (HERE / "dataset" / "val.jsonl").read_text().splitlines() if l.strip()]
    rows = rows[:args.n]

    print(">>> loading BASE (no adapter) ...", flush=True)
    cm = ChatModel(dict(model_name_or_path=BASE, template="qwen3_5",
                        trust_remote_code=False, infer_backend="huggingface"))
    base_out = gen_all(cm, rows, args.max_new_tokens)
    del cm
    gc.collect(); torch.cuda.empty_cache()

    print(">>> loading BASE + adapter-8k-v2 ...", flush=True)
    cm = ChatModel(dict(model_name_or_path=BASE, adapter_name_or_path=ADAPTER,
                        template="qwen3_5", trust_remote_code=False,
                        infer_backend="huggingface"))
    adap_out = gen_all(cm, rows, args.max_new_tokens)
    del cm
    gc.collect(); torch.cuda.empty_cache()

    scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)
    agg = {"base": {"r": [], "n": []}, "adap": {"r": [], "n": []}}
    for ex, b, a in zip(rows, base_out, adap_out):
        ref = ex["output"]
        agg["base"]["r"].append(scorer.score(ref, b)["rougeL"].fmeasure)
        agg["base"]["n"].append(number_recall(ref, b))
        agg["adap"]["r"].append(scorer.score(ref, a)["rougeL"].fmeasure)
        agg["adap"]["n"].append(number_recall(ref, a))

    def avg(x):
        return sum(x) / max(len(x), 1)
    print("\n===== A/B RESULT (LLaMA-Factory inference, held-out companies) =====")
    print(f"{'metric':14} {'base (no FT)':>14} {'+ 8-K adapter':>14}")
    print(f"{'ROUGE-L':14} {avg(agg['base']['r']):>14.3f} {avg(agg['adap']['r']):>14.3f}")
    print(f"{'number-recall':14} {avg(agg['base']['n']):>14.3f} {avg(agg['adap']['n']):>14.3f}")
    print(f"{'avg len (char)':14} {avg([len(x) for x in base_out]):>14.0f} {avg([len(x) for x in adap_out]):>14.0f}")

    with open(HERE / "eval_samples_lf.txt", "w") as fh:
        for ex, b, a in list(zip(rows, base_out, adap_out))[:6]:
            fh.write(f"===== {ex['ticker']} Item {ex['item']} ({ex.get('deal_type')}) =====\n")
            fh.write(f"--- REAL ---\n{ex['output']}\n\n--- BASE ---\n{b}\n\n--- ADAPTER ---\n{a}\n\n\n")
    print(f"\nWrote samples -> {HERE/'eval_samples_lf.txt'}")


if __name__ == "__main__":
    main()
