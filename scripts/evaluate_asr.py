#!/usr/bin/env python3
"""Evaluate a trained NeMo ASR model on the SADA test set.

Reports Word Error Rate (WER) and Character Error Rate (CER),
with optional per-dialect breakdown.

Usage:
    python scripts/evaluate_asr.py --model experiments/FastConformer-CTC-BPE-Medium-Arabic_final.nemo \
                                   --test-manifest manifests/test_manifest.json

    # With dialect breakdown (requires metadata):
    python scripts/evaluate_asr.py --model model.nemo \
                                   --test-manifest manifests/test_manifest.json \
                                   --metadata data/test/metadata.tsv
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import torch
from jiwer import cer, wer
from tqdm import tqdm

import nemo.collections.asr as nemo_asr
from nemo.utils import logging

from backend.ai.audio_preprocessing import preprocess_audio_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate NeMo ASR model")
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Path to .nemo model checkpoint",
    )
    parser.add_argument(
        "--test-manifest",
        type=str,
        default="manifests/test_manifest.json",
        help="Path to test manifest JSON-lines file",
    )
    parser.add_argument(
        "--metadata",
        type=str,
        default=None,
        help="Optional TSV with dialect/gender/age metadata for per-group breakdown",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Inference batch size (default: 8)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write per-sample results to this JSON file",
    )
    return parser.parse_args()


def load_manifest(path: str) -> list[dict]:
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def load_metadata_index(tsv_path: str) -> dict[str, dict]:
    """Build an index from audio filepath -> metadata row."""
    index = {}
    with open(tsv_path, "r", encoding="utf-8") as f:
        header = f.readline().strip().split("\t")
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) != len(header):
                continue
            row = dict(zip(header, parts))
            audio_path = row.get("audio_filepath", "")
            index[audio_path] = row
            index[Path(audio_path).name] = row
    return index


def compute_metrics(references: list[str], hypotheses: list[str]) -> dict:
    """Compute WER and CER."""
    valid_refs, valid_hyps = [], []
    for r, h in zip(references, hypotheses):
        if r.strip():
            valid_refs.append(r)
            valid_hyps.append(h if h.strip() else "")

    if not valid_refs:
        return {"wer": 1.0, "cer": 1.0, "count": 0}

    return {
        "wer": wer(valid_refs, valid_hyps),
        "cer": cer(valid_refs, valid_hyps),
        "count": len(valid_refs),
    }


def main():
    args = parse_args()

    if not os.path.isfile(args.model):
        logging.error(f"Model not found: {args.model}")
        sys.exit(1)
    if not os.path.isfile(args.test_manifest):
        logging.error(f"Test manifest not found: {args.test_manifest}")
        sys.exit(1)

    # ── Load model ───────────────────────────────────────────────────
    logging.info(f"Loading model from {args.model}")
    asr_model = nemo_asr.models.EncDecCTCModelBPE.restore_from(args.model)
    asr_model.eval()

    if torch.cuda.is_available():
        asr_model = asr_model.cuda()
        logging.info(f"Running on GPU: {torch.cuda.get_device_name(0)}")
    else:
        logging.warning("No GPU detected, running on CPU")

    # ── Load test data ───────────────────────────────────────────────
    manifest_entries = load_manifest(args.test_manifest)
    preprocessed_dir = Path("logs") / "evaluation_preprocessed_audio"
    preprocessed_dir.mkdir(parents=True, exist_ok=True)
    audio_paths = []
    for entry in manifest_entries:
        output_path = preprocessed_dir / f"{Path(entry['audio_filepath']).stem}.preprocessed.wav"
        processed_path, _ = preprocess_audio_file(entry["audio_filepath"], output_path)
        audio_paths.append(str(processed_path))
    references = [e["text"] for e in manifest_entries]
    logging.info(f"Test samples: {len(manifest_entries)}")

    # ── Run inference ────────────────────────────────────────────────
    logging.info("Running inference...")
    hypotheses = asr_model.transcribe(audio_paths, batch_size=args.batch_size)

    if isinstance(hypotheses, tuple):
        hypotheses = hypotheses[0]

    # ── Overall metrics ──────────────────────────────────────────────
    overall = compute_metrics(references, hypotheses)
    print("\n" + "=" * 60)
    print("OVERALL RESULTS")
    print("=" * 60)
    print(f"  Samples : {overall['count']}")
    print(f"  WER     : {overall['wer'] * 100:.2f}%")
    print(f"  CER     : {overall['cer'] * 100:.2f}%")
    print("=" * 60)

    # ── Per-dialect breakdown ────────────────────────────────────────
    if args.metadata and os.path.isfile(args.metadata):
        meta_index = load_metadata_index(args.metadata)
        dialect_refs = defaultdict(list)
        dialect_hyps = defaultdict(list)

        for entry, hyp in zip(manifest_entries, hypotheses):
            audio_key = Path(entry["audio_filepath"]).name
            meta = meta_index.get(audio_key, {})
            dialect = meta.get("dialect", "unknown")
            dialect_refs[dialect].append(entry["text"])
            dialect_hyps[dialect].append(hyp)

        print("\nPER-DIALECT BREAKDOWN")
        print("-" * 60)
        for dialect in sorted(dialect_refs.keys()):
            m = compute_metrics(dialect_refs[dialect], dialect_hyps[dialect])
            print(f"  {dialect:20s}  WER={m['wer']*100:6.2f}%  CER={m['cer']*100:6.2f}%  (n={m['count']})")
        print("-" * 60)

    # ── Write per-sample results ─────────────────────────────────────
    if args.output:
        results = []
        for entry, ref, hyp in zip(manifest_entries, references, hypotheses):
            results.append({
                "audio_filepath": entry["audio_filepath"],
                "reference": ref,
                "hypothesis": hyp,
                "wer": wer([ref], [hyp]) if ref.strip() else None,
                "cer": cer([ref], [hyp]) if ref.strip() else None,
            })
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logging.info(f"Per-sample results written to {args.output}")


if __name__ == "__main__":
    main()
