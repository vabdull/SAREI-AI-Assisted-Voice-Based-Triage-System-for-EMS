#!/usr/bin/env python3
"""Convert SADA metadata TSVs into NeMo-compatible JSON-line manifest files.

Uses the pre-cleaned ``cleaned_text`` column from SADA by default, which is
already diacritic-free, punctuation-free, and normalised for Arabic ASR.

Usage:
    python scripts/prepare_manifests.py
    python scripts/prepare_manifests.py --data-dir data --output-dir manifests
"""

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prepare NeMo manifests from SADA")
    p.add_argument("--data-dir", type=str, default="data")
    p.add_argument("--output-dir", type=str, default="manifests")
    p.add_argument("--min-duration", type=float, default=0.3)
    p.add_argument("--max-duration", type=float, default=20.0)
    return p.parse_args()


def clean_text(text: str) -> str:
    """Light safety pass — the SADA cleaned_text is already good, but we
    guard against any stray characters that would hurt CTC training."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def read_metadata(tsv_path: Path) -> list[dict]:
    rows = []
    with open(tsv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            if row:
                rows.append(row)
    return rows


def build_manifest(rows: list[dict], split: str, out_dir: Path,
                   args: argparse.Namespace) -> dict:
    path = out_dir / f"{split}_manifest.json"
    stats = Counter(total=0, kept=0, short=0, long=0, empty=0, bad=0)
    duration_h = 0.0

    with open(path, "w", encoding="utf-8", newline="") as f:
        for row in tqdm(rows, desc=f"  {split}"):
            stats["total"] += 1
            try:
                dur = float(row["duration"])
            except (ValueError, TypeError):
                stats["bad"] += 1
                continue
            if dur < args.min_duration:
                stats["short"] += 1
                continue
            if dur > args.max_duration:
                stats["long"] += 1
                continue

            text = clean_text(row.get("cleaned_text") or row["text"])
            if not text:
                stats["empty"] += 1
                continue

            audio = row["audio_filepath"]
            if not os.path.isfile(audio):
                alt = Path(args.data_dir) / split / "audio" / Path(audio).name
                if alt.is_file():
                    audio = str(alt)
                else:
                    continue

            json.dump({"audio_filepath": audio, "duration": round(dur, 4),
                       "text": text}, f, ensure_ascii=False)
            f.write("\n")
            stats["kept"] += 1
            duration_h += dur / 3600

    print(f"  {split}: {stats['kept']}/{stats['total']} kept "
          f"({duration_h:.1f}h) | skip: {stats['short']} short, "
          f"{stats['long']} long, {stats['empty']} empty, "
          f"{stats['bad']} bad rows")
    return dict(stats, duration_h=duration_h)


def collect_vocab(manifest: Path) -> set[str]:
    chars: set[str] = set()
    with open(manifest, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                chars.update(json.loads(line)["text"])
    return chars


def main():
    args = parse_args()
    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Preparing NeMo manifests from SADA (using cleaned_text)")
    print(f"  Data dir : {data_dir.resolve()}")
    print(f"  Output   : {out_dir.resolve()}")
    print(f"  Duration : {args.min_duration}s – {args.max_duration}s\n")

    for split in ["train", "validation", "test"]:
        tsv = data_dir / split / "metadata.tsv"
        if not tsv.exists():
            print(f"  [SKIP] {tsv}")
            continue
        rows = read_metadata(tsv)
        build_manifest(rows, split, out_dir, args)

    train_manifest = out_dir / "train_manifest.json"
    if train_manifest.exists():
        vocab = collect_vocab(train_manifest)
        print(f"\nTraining vocab: {len(vocab)} unique characters")
        vocab_path = out_dir / "vocab_chars.txt"
        with open(vocab_path, "w", encoding="utf-8") as f:
            for ch in sorted(vocab):
                f.write(f"{ch}\t{unicodedata.name(ch, 'UNKNOWN')}\n")
        print(f"Saved to {vocab_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
