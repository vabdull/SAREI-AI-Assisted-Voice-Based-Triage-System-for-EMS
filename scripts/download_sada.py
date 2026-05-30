#!/usr/bin/env python3
"""Download the SADA22 Arabic speech dataset from HuggingFace.

Usage:
    python scripts/download_sada.py [--output-dir data] [--num-proc 4]
"""

import argparse
import os
import sys
from pathlib import Path

from datasets import Audio, load_dataset
from tqdm import tqdm


DATASET_ID = "MohamedRashad/SADA22"
SPLITS = ["train", "validation", "test"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download SADA22 dataset")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data",
        help="Root directory to save audio files and metadata (default: data)",
    )
    parser.add_argument(
        "--num-proc",
        type=int,
        default=4,
        help="Number of parallel processes for audio extraction (default: 4)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="Target sample rate in Hz (default: 16000)",
    )
    return parser.parse_args()


def save_split(dataset_split, split_name: str, output_dir: Path, target_sr: int):
    """Save audio files and build a raw metadata TSV for one split."""
    split_dir = output_dir / split_name / "audio"
    split_dir.mkdir(parents=True, exist_ok=True)

    meta_path = output_dir / split_name / "metadata.tsv"
    total_duration = 0.0

    with open(meta_path, "w", encoding="utf-8") as meta_f:
        meta_f.write("file_id\taudio_filepath\tduration\ttext\tcleaned_text\tdialect\tgender\tage\n")

        for idx, sample in enumerate(tqdm(dataset_split, desc=f"  {split_name}")):
            audio = sample["audio"]
            sr = audio["sampling_rate"]
            array = audio["array"]
            duration = len(array) / sr

            file_id = f"{split_name}_{idx:07d}"
            wav_path = split_dir / f"{file_id}.wav"

            if not wav_path.exists():
                import soundfile as sf
                import numpy as np

                audio_array = np.array(array, dtype=np.float32)

                if sr != target_sr:
                    import librosa
                    audio_array = librosa.resample(
                        audio_array, orig_sr=sr, target_sr=target_sr
                    )
                    duration = len(audio_array) / target_sr

                sf.write(str(wav_path), audio_array, target_sr)

            text = sample.get("text", "")
            cleaned_text = sample.get("cleaned_text", "")
            dialect = sample.get("speaker_dialect", "unknown")
            gender = sample.get("speaker_gender", "unknown")
            age = sample.get("speaker_age", "unknown")

            meta_f.write(
                f"{file_id}\t{wav_path}\t{duration:.4f}\t{text}\t{cleaned_text}\t{dialect}\t{gender}\t{age}\n"
            )
            total_duration += duration

    hours = total_duration / 3600
    print(f"  {split_name}: {len(dataset_split)} samples, {hours:.1f} hours -> {split_dir}")
    return total_duration


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {DATASET_ID} ...")
    print(f"Output directory: {output_dir.resolve()}")
    print(f"Target sample rate: {args.sample_rate} Hz")
    print()

    dataset = load_dataset(DATASET_ID, num_proc=args.num_proc)
    # Force soundfile decoder instead of torchcodec
    dataset = dataset.cast_column("audio", Audio(sampling_rate=args.sample_rate, decode=True))

    grand_total = 0.0
    for split_name in SPLITS:
        if split_name not in dataset:
            print(f"  [SKIP] Split '{split_name}' not found in dataset")
            continue
        dur = save_split(dataset[split_name], split_name, output_dir, args.sample_rate)
        grand_total += dur

    print(f"\nDone! Total audio: {grand_total / 3600:.1f} hours")
    print(f"Data saved to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
