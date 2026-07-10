#!/usr/bin/env python
"""Convert Law_RAG's prepared 8-K pairs (train.jsonl / val.jsonl) into
LLaMA-Factory alpaca-format JSON, faithfully reproducing train_lora.py's prompt:

  system : SYSTEM (the securities-lawyer instruction)
  prompt : <instruction>\n\n=== SOURCE DOCUMENT ===\n<input>
  input  : ""   (unused; everything is folded into prompt)
  output : <output>  (the real Item disclosure — the only supervised span)

LLaMA-Factory masks the prompt and trains only on `output` (completion-only),
matching train_lora.py's DataCollatorForCompletionOnlyLM.
"""
import json
from pathlib import Path

SRC_DIR = Path("/home/thematrix/Law_RAG/training/dataset")
OUT_DIR = Path("/mnt/raid/law_rag_8k/data")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM = ("You are a securities lawyer drafting U.S. SEC Form 8-K Item disclosures. "
          "Write in the concise, neutral style of a real filing, disclosing only "
          "material terms and using only facts present in the provided source document.")


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def convert(rows):
    out = []
    for r in rows:
        prompt = r["instruction"] + "\n\n=== SOURCE DOCUMENT ===\n" + r["input"]
        out.append({
            "system": SYSTEM,
            "instruction": prompt,
            "input": "",
            "output": r["output"],
        })
    return out


for split, src_name in (("train", "train.jsonl"), ("val", "val.jsonl")):
    rows = load_jsonl(SRC_DIR / src_name)
    conv = convert(rows)
    dst = OUT_DIR / f"lawrag_8k_{split}.json"
    dst.write_text(json.dumps(conv, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{split}: {len(conv)} examples -> {dst}")

# dataset_info.json so LLaMA-Factory can resolve the dataset by name
info = {
    "lawrag_8k_train": {
        "file_name": "lawrag_8k_train.json",
        "columns": {"prompt": "instruction", "query": "input",
                    "response": "output", "system": "system"},
    },
    "lawrag_8k_val": {
        "file_name": "lawrag_8k_val.json",
        "columns": {"prompt": "instruction", "query": "input",
                    "response": "output", "system": "system"},
    },
}
(OUT_DIR / "dataset_info.json").write_text(
    json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {OUT_DIR/'dataset_info.json'}")
