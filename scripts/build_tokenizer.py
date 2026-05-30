#!/usr/bin/env python3
"""Train a SentencePiece BPE tokenizer on Arabic transcriptions.

Defaults to the dialect-filtered training manifest so the tokenizer
vocabulary matches exactly what the model will see during training.

Usage:
    python scripts/build_tokenizer.py
    python scripts/build_tokenizer.py --manifest manifests_ems_dialects/train_manifest.json
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import sentencepiece as spm


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train BPE tokenizer for Arabic ASR")
    p.add_argument("--manifest", type=str,
                   default="manifests_ems_dialects/train_manifest.json")
    p.add_argument("--output-dir", type=str, default="tokenizer")
    p.add_argument("--vocab-size", type=int, default=256)
    p.add_argument("--character-coverage", type=float, default=1.0)
    p.add_argument("--model-type", type=str, default="bpe",
                   choices=["bpe", "unigram", "char", "word"])
    return p.parse_args()


def extract_texts(manifest: str) -> list[str]:
    texts = []
    with open(manifest, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            text = json.loads(line).get("text", "")
            if text:
                texts.append(text)
    return texts


def main():
    args = parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Training SentencePiece {args.model_type.upper()} tokenizer")
    print(f"  Manifest  : {args.manifest}")
    print(f"  Vocab size: {args.vocab_size}")
    print(f"  Output    : {out.resolve()}\n")

    if not os.path.isfile(args.manifest):
        print(f"ERROR: Manifest not found: {args.manifest}")
        print("Run prepare_manifests.py and filter_dialect.py first.")
        sys.exit(1)

    texts = extract_texts(args.manifest)
    print(f"  Loaded {len(texts):,} transcriptions")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        for t in texts:
            tmp.write(t + "\n")
        tmp_path = tmp.name

    prefix = str(out / "tokenizer")

    try:
        spm.SentencePieceTrainer.train(
            input=tmp_path,
            model_prefix=prefix,
            vocab_size=args.vocab_size,
            character_coverage=args.character_coverage,
            model_type=args.model_type,
            pad_id=0, unk_id=1, bos_id=2, eos_id=3,
            max_sentence_length=16384,
            num_threads=os.cpu_count() or 4,
            train_extremely_large_corpus=len(texts) > 500_000,
        )
    finally:
        os.unlink(tmp_path)

    sp = spm.SentencePieceProcessor()
    sp.load(f"{prefix}.model")
    print(f"\n  Vocab size: {sp.get_piece_size()}")

    if texts:
        sample = texts[0][:80]
        ids = sp.encode(sample, out_type=int)
        decoded = sp.decode(ids)
        print(f"\n  Sample : {sample}")
        print(f"  Tokens : {sp.encode(sample, out_type=str)}")
        print(f"  IDs    : {ids}")
        print(f"  Decode : {decoded}")
        print(f"  Match  : {'OK' if decoded == sample else 'MISMATCH'}")

    vocab_txt = out / "vocab.txt"
    with open(vocab_txt, "w", encoding="utf-8") as f:
        for i in range(sp.get_piece_size()):
            f.write(sp.id_to_piece(i) + "\n")

    print(f"\n  Files: {prefix}.model, {prefix}.vocab, {vocab_txt}")
    print("Tokenizer training complete.")


if __name__ == "__main__":
    main()
