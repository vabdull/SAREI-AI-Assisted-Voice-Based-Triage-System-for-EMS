#!/usr/bin/env python3
"""Transcribe audio files using a trained NeMo ASR model.

Usage:
    # Single file
    python scripts/transcribe.py --model model.nemo --audio recording.wav

    # Multiple files
    python scripts/transcribe.py --model model.nemo --audio file1.wav file2.wav file3.wav

    # All WAV files in a directory
    python scripts/transcribe.py --model model.nemo --audio-dir recordings/

    # Output to JSON
    python scripts/transcribe.py --model model.nemo --audio recording.wav --output results.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

import torch

import nemo.collections.asr as nemo_asr
from nemo.utils import logging

from backend.ai.audio_preprocessing import preprocess_audio_file


SUPPORTED_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".opus"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe audio with NeMo ASR")
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Path to .nemo model file",
    )
    parser.add_argument(
        "--audio",
        type=str,
        nargs="*",
        default=None,
        help="One or more audio file paths",
    )
    parser.add_argument(
        "--audio-dir",
        type=str,
        default=None,
        help="Directory containing audio files to transcribe",
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
        help="Write results to this JSON file instead of stdout",
    )
    return parser.parse_args()


def collect_audio_files(args: argparse.Namespace) -> list[str]:
    """Collect audio file paths from --audio and --audio-dir arguments."""
    files = []

    if args.audio:
        for p in args.audio:
            if os.path.isfile(p):
                files.append(p)
            else:
                logging.warning(f"File not found, skipping: {p}")

    if args.audio_dir:
        audio_dir = Path(args.audio_dir)
        if audio_dir.is_dir():
            for ext in SUPPORTED_EXTENSIONS:
                files.extend(str(f) for f in sorted(audio_dir.glob(f"*{ext}")))
        else:
            logging.warning(f"Directory not found: {args.audio_dir}")

    return files


def main():
    args = parse_args()

    if not os.path.isfile(args.model):
        logging.error(f"Model not found: {args.model}")
        sys.exit(1)

    audio_files = collect_audio_files(args)
    if not audio_files:
        logging.error("No audio files specified. Use --audio or --audio-dir.")
        sys.exit(1)

    logging.info(f"Loading model: {args.model}")
    asr_model = nemo_asr.models.EncDecCTCModelBPE.restore_from(args.model)
    asr_model.eval()

    if torch.cuda.is_available():
        asr_model = asr_model.cuda()
        logging.info(f"Running on GPU: {torch.cuda.get_device_name(0)}")

    preprocessed_dir = Path("logs") / "batch_preprocessed_audio"
    preprocessed_dir.mkdir(parents=True, exist_ok=True)
    preprocessed_files = []
    preprocessing_metadata = {}
    for audio_file in audio_files:
        output_path = preprocessed_dir / f"{Path(audio_file).stem}.preprocessed.wav"
        processed_path, metadata = preprocess_audio_file(audio_file, output_path)
        preprocessed_files.append(str(processed_path))
        preprocessing_metadata[audio_file] = {
            "original_sample_rate": metadata.original_sample_rate,
            "output_sample_rate": metadata.output_sample_rate,
            "original_duration_seconds": metadata.original_duration_seconds,
            "output_duration_seconds": metadata.output_duration_seconds,
            "clipped_samples_detected": metadata.clipped_samples_detected,
            "used_noise_reduction": metadata.used_noise_reduction,
            "used_vad": metadata.used_vad,
        }

    logging.info(f"Transcribing {len(preprocessed_files)} file(s)...")
    hypotheses = asr_model.transcribe(preprocessed_files, batch_size=args.batch_size)

    if isinstance(hypotheses, tuple):
        hypotheses = hypotheses[0]

    results = []
    for audio_path, text in zip(audio_files, hypotheses):
        results.append(
            {
                "audio": audio_path,
                "text": text,
                "preprocessing": preprocessing_metadata.get(audio_path, {}),
            }
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logging.info(f"Results written to {args.output}")
    else:
        print()
        for r in results:
            print(f"[{Path(r['audio']).name}]")
            print(f"  {r['text']}")
            print()


if __name__ == "__main__":
    main()
